import json
from pathlib import Path

import pytest

from evaluation.performance import run_offline_benchmark, write_report_bundle
from evaluation.quality import (
    evaluate_fixture_predictions, load_jsonl, match_case_findings, run_live_evaluation, summarize_quality,
)
from evaluation.security_corpus import load_corpus, run_corpus


def test_security_corpus_covers_rules_clean_controls_and_declared_unsupported_patterns():
    cases = load_corpus()
    assert len(cases) >= 15
    assert {finding["rule_id"] for case in cases for finding in case["expected_findings"]} == {
        "PYSEC001", "PYSEC002", "PYSEC003", "PYSEC004", "PYSEC005",
    }
    assert any(case["unsupported_patterns"] for case in cases)
    assert all(case["provenance"] == "synthetic_manual" for case in cases)
    finding_keys = [label["finding_key"] for case in cases for label in case["expected_findings"]]
    assert len(finding_keys) == len(set(finding_keys))
    report = run_corpus(cases)
    assert report["case_count"] == len(cases)
    assert report["false_positives"] == report["false_negatives"] == 0
    assert "not real-world" in report["limitations"]


def test_quality_matching_is_one_to_one_by_rule_and_location_and_scores_severity_separately():
    truth = [
        {"rule_id": "PYSEC001", "category": "security", "severity": "high", "line_start": 10},
        {"rule_id": "PYSEC001", "category": "security", "severity": "high", "line_start": 20},
    ]
    predictions = [
        {"rule_id": "PYSEC001", "issue_type": "security", "severity": "low", "line_start": 11},
        {"rule_id": "PYSEC001", "issue_type": "security", "severity": "high", "line_start": 20},
        {"rule_id": "PYSEC001", "issue_type": "security", "severity": "high", "line_start": 20},
    ]
    result = match_case_findings(truth, predictions)
    assert result["true_positives"] == 2
    assert result["false_positives"] == 1 and result["false_negatives"] == 0
    assert [item["severity_correct"] for item in result["matches"]] == [False, True]


def test_missing_or_failed_offline_predictions_are_not_false_negatives():
    cases = [{"case_id": "a", "ground_truth": [{"category": "security"}]},
             {"case_id": "b", "ground_truth": []},
             {"case_id": "c", "ground_truth": []}]
    rows = evaluate_fixture_predictions(cases, {
        "a": {"case_id": "a", "findings": [], "status": "completed"},
        "b": {"case_id": "b", "status": "failed", "error": "fixture error"},
    })
    metrics = summarize_quality(rows, len(cases))
    assert metrics["micro"]["false_negatives"] == 1
    assert metrics["timing_seconds"]["count"] == 0
    assert metrics["case_counts"] == {"dataset": 3, "evaluated": 1, "failed": 1,
                                      "skipped": 1, "coverage": pytest.approx(1 / 3)}


def test_empty_denominators_are_explicit_nulls():
    metrics = summarize_quality([], 0)
    assert metrics["micro"]["precision"] is None
    assert metrics["micro"]["recall"] is None
    assert metrics["macro"]["f1"] is None
    assert metrics["false_positives_per_reviewed_file"] is None
    assert metrics["severity_classification_accuracy"] is None


def test_quality_f1_is_zero_for_evaluable_positive_cases_with_no_prediction():
    rows = evaluate_fixture_predictions(
        [{"case_id": "miss", "ground_truth": [{"rule_id": "PYSEC001", "line_start": 1}]}],
        {"miss": {"case_id": "miss", "findings": [], "status": "completed"}},
    )
    metrics = summarize_quality(rows, 1)
    assert metrics["micro"]["precision"] is None
    assert metrics["micro"]["recall"] == 0.0
    assert metrics["micro"]["f1"] == 0.0
    assert metrics["macro"]["f1"] == 0.0


def test_category_only_ground_truth_can_match_findings_with_additional_rule_metadata():
    result = match_case_findings(
        [{"category": "security", "line_start": 4}],
        [{"issue_type": "security", "rule_id": "PYSEC001", "line_start": 4}],
    )
    assert result["true_positives"] == 1


def test_live_quality_cli_requires_explicit_opt_in(monkeypatch, capsys):
    import sys
    import evaluation.quality as quality

    monkeypatch.setattr(sys, "argv", ["quality", "--mode", "live-model"])
    with pytest.raises(SystemExit) as error:
        quality.main()
    assert error.value.code == 2
    assert "requires --allow-live-model" in capsys.readouterr().err


def test_quality_dataset_is_small_synthetic_and_documented():
    cases = load_jsonl(Path("evaluation/quality_cases_v1.jsonl"))
    assert cases and all(case["provenance"] == "synthetic_manual" for case in cases)
    assert all("ground_truth" in case and "source_code" in case for case in cases)


def test_injected_live_evaluation_uses_shared_pipeline_and_only_scores_llm_origin():
    class OfflineProvider:
        model = "offline-injected-provider"

        def analyze_code(self, code, context):
            return json.dumps({"summary": "fixture", "findings": []})

    case = {"case_id": "merge", "source_code": "value = eval(source)\n",
            "ground_truth": [{"rule_id": "PYSEC001", "category": "security",
                              "severity": "high", "line_start": 1}]}
    rows = run_live_evaluation([case], provider=OfflineProvider(), provider_identity="mock", rag_enabled=False)
    assert rows[0]["status"] == "completed"
    assert rows[0]["findings"] == []  # static finding does not masquerade as a model prediction
    assert rows[0]["matching"]["false_negatives"] == 1
    assert rows[0]["retrieval_status"] == "not_run"


def test_live_rag_evaluation_reports_unknown_evidence_and_retrieval_diagnostics(monkeypatch):
    from review_schema import RetrievedEvidence

    class InvalidEvidenceProvider:
        def analyze_code(self, code, context):
            return json.dumps({"summary": "bad evidence fixture", "findings": [{
                "title": "Unsupported cite", "issue_type": "security", "explanation": "Fixture.",
                "recommended_fix": "Check.", "line_start": 1, "evidence_ids": ["missing"],
                "uncertain": False, "abstained": False,
            }]})

    evidence = RetrievedEvidence(evidence_id="known", source_family="fixture", source_url=None,
                                 rule_identifier=None, text="trusted reference data")
    case = {"case_id": "rag", "source_code": "x = 1\n", "ground_truth": []}
    # Inject the retriever through the shared orchestrator for this isolated test.
    from mcp_server import orchestrator
    original = orchestrator.run_review_pipeline

    def with_evidence(**kwargs):
        kwargs["retriever"] = lambda _code: [evidence]
        return original(**kwargs)

    # The public harness normally resolves configured retrieval; monkeypatching
    # the shared entry point proves unknown IDs survive semantic validation.
    monkeypatch.setattr(orchestrator, "run_review_pipeline", with_evidence)
    rows = run_live_evaluation([case], provider=InvalidEvidenceProvider(), provider_identity="mock",
                               rag_enabled=True)
    assert rows[0]["status"] == "failed"
    assert rows[0]["retrieval_status"] == "passed" and rows[0]["retrieved_evidence_count"] == 1
    assert rows[0]["evidence_validation_status"] == "failed"
    metrics = summarize_quality(rows, 1)
    assert metrics["rag"]["retrieval_success_rate"] == 1.0
    assert metrics["rag"]["retrieved_chunks"] == 1
    assert metrics["rag"]["evidence_validation_failure_rate"] == 1.0


def test_offline_performance_benchmark_reports_mocked_stages_and_does_not_call_network():
    report = run_offline_benchmark(runs=3, warmups=1)
    assert report["evaluation_mode"] == "offline_mocked_provider"
    assert report["successful_requests"] == 3 and report["failed_requests"] == 0
    assert report["timings"]["request_validation_seconds"]["count"] == 3
    for step in ("rag_retrieval_seconds", "model_generation_seconds", "response_validation_seconds",
                 "static_analysis_seconds", "total_review_seconds", "fix_verification_seconds"):
        assert report["timings"][step]["count"] == 3
        assert report["timings"][step]["median"] is not None
    assert "mocked" in report["limitations"]


def test_report_bundle_is_machine_readable_and_never_overwrites(tmp_path):
    report = run_offline_benchmark(runs=1, warmups=0)
    destination = tmp_path / "run-unique"
    write_report_bundle(destination, report)
    assert json.loads((destination / "report.json").read_text())["evaluation_mode"] == "offline_mocked_provider"
    assert (destination / "case_results.csv").is_file() and (destination / "summary.md").is_file()
    with pytest.raises(FileExistsError):
        write_report_bundle(destination, report)
