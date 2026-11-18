import json

from evaluation.phase_e3_4_analysis import _paired_v3_comparison, build_artifacts
from evaluation.runner import load_cases


def test_pending_e3_4_analysis_uses_nulls_not_synthetic_zeroes(tmp_path):
    summary = build_artifacts(results_dir=tmp_path)
    assert summary["status"] == "pending_or_incomplete"
    for config in summary["configurations"].values():
        assert config["status"] == "pending"
        assert config["metrics"] is None
        assert config["completed_cases"] is None
    comparison = json.loads((tmp_path / "phase_e3_4_case_comparison.json").read_text())
    assert comparison["case_count"] == 30
    assert all(case["configurations"]["no_rag"] is None for case in comparison["cases"])
    assert all(value is None for value in comparison["cases"][0]["observed_changes_vs_v2"].values())
    annotations = [json.loads(line) for line in
                   (tmp_path / "phase_e3_4_grounding_annotation_template.jsonl").read_text().splitlines()]
    assert len(annotations) == 3
    assert all(row["annotation_status"] == "awaiting_live_result" for row in annotations)


def test_run2_analysis_uses_isolated_artifact_names(tmp_path):
    summary = build_artifacts(results_dir=tmp_path, run2=True)
    assert summary["experiment"] == "phase_e3_4_run2"
    assert summary["selected_arms"] == ["v3_baseline", "v3_optimized"]
    assert set(summary["configurations"]) == {"v3_baseline", "v3_optimized"}
    assert summary["paired_v3_comparison"]["provisional"] is True
    assert summary["paired_v3_comparison"]["paired_case_count"] == 30
    assert summary["paired_v3_comparison"]["case_outcome_counts"] == {
        "neither_result_available": 30,
    }
    assert (tmp_path / "summary.json").is_file()
    assert (tmp_path / "case_comparison.json").is_file()
    assert (tmp_path / "grounding_annotation_template.jsonl").is_file()
    assert (tmp_path / "evaluation_report.md").is_file()
    assert (tmp_path / "quota_usage_report.json").is_file()
    assert not (tmp_path / "phase_e3_4_summary.json").exists()
    comparison = json.loads((tmp_path / "case_comparison.json").read_text())
    assert len(comparison["cases"]) == 30
    assert all(set(case["configurations"]) == {"v3_baseline", "v3_optimized"}
               for case in comparison["cases"])
    report = (tmp_path / "evaluation_report.md").read_text()
    assert "Historical Reference Results — Not Part of the Controlled Run 2 Comparison" in report


def test_run2_subset_analysis_reports_historical_files_separately(tmp_path):
    (tmp_path / "plan.json").write_text(json.dumps({"selected_arms": ["v3_optimized", "v3_baseline"]}))
    history = tmp_path.parent / "phase_e2_30_no_rag.json"
    history.write_text(json.dumps({"cases_run": 30, "model_id": "historical-model",
                                   "metrics": {"finding_precision": 0.75}}))
    try:
        summary = build_artifacts(results_dir=tmp_path, run2=True)
    finally:
        history.unlink(missing_ok=True)
    assert summary["selected_arms"] == ["v3_baseline", "v3_optimized"]
    assert "phase_e2_no_rag" in summary["historical_reference_results"]
    assert "no_rag" not in summary["configurations"]


def test_paired_v3_comparison_classifies_partial_and_quality_outcomes():
    case_ids = [case["case_id"] for case in load_cases()]
    baseline = {"case_results": [
        {"case_id": case_ids[0], "case_status": "completed", "schema_valid": True,
         "matching": {"true_positives": 1, "false_positives": 0, "false_negatives": 0}},
        {"case_id": case_ids[1], "case_status": "failed", "schema_valid": False,
         "schema_errors": ["invalid response"], "provider_errors": [], "matching": {}},
        {"case_id": case_ids[3], "case_status": "failed", "schema_valid": False,
         "schema_errors": ["invalid response"], "provider_errors": [], "matching": {}},
    ], "metrics": {"finding_precision": 1.0}}
    optimized = {"case_results": [
        {"case_id": case_ids[0], "case_status": "completed", "schema_valid": True,
         "matching": {"true_positives": 1, "false_positives": 1, "false_negatives": 0}},
        {"case_id": case_ids[1], "case_status": "completed", "schema_valid": True,
         "matching": {"true_positives": 1, "false_positives": 0, "false_negatives": 0}},
        {"case_id": case_ids[2], "case_status": "completed", "schema_valid": True,
         "matching": {"true_positives": 0, "false_positives": 0, "false_negatives": 1}},
        {"case_id": case_ids[3], "case_status": "failed", "schema_valid": False,
         "schema_errors": ["invalid response"], "provider_errors": [], "matching": {}},
    ], "metrics": {"finding_precision": 0.5}}

    comparison = _paired_v3_comparison(baseline, optimized, case_ids[:5])

    assert comparison["provisional"] is True
    assert comparison["complete_paired_case_count"] == 1
    assert comparison["cases"][0]["false_positive_optimized_only"] is True
    assert comparison["cases"][1]["invalid_schema_baseline_only"] is True
    assert comparison["cases"][2]["outcome"] == "optimized_only_result"
    assert comparison["cases"][3]["outcome"] == "both_fail"
    assert comparison["cases"][4]["outcome"] == "neither_result_available"
    assert comparison["comparison_basis_case_ids"] == [case_ids[0]]
    assert comparison["paired_quality"]["baseline"]["precision"] == 1.0
    assert comparison["paired_quality"]["optimized"]["precision"] == 0.5
    assert comparison["partially_paired_case_count"] == 2
    assert comparison["pending_case_count"] == 2


def test_final_paired_analysis_uses_all_thirty_common_completed_cases():
    cases = load_cases()
    rows = [{"case_id": case["case_id"], "case_status": "completed", "schema_valid": True,
             "evidence_references_valid": True, "category": case["category"],
             "review_latency_seconds": 0.25, "timings": {"retrieval_seconds": 0.05},
             "matching": {"true_positives": 1, "false_positives": 0, "false_negatives": 0}}
            for case in cases]
    baseline = {"case_results": rows, "interrupted": False}
    optimized = {"case_results": rows, "interrupted": False}
    comparison = _paired_v3_comparison(baseline, optimized, [case["case_id"] for case in cases])
    assert comparison["provisional"] is False
    assert comparison["complete_paired_case_count"] == 30
    assert len(comparison["comparison_basis_case_ids"]) == 30
    assert comparison["optimized_minus_baseline"]["precision"] == 0
