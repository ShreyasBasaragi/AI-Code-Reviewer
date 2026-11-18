import json

from evaluation.runner import run_evaluation
from review_schema import RetrievedEvidence


CASE = {
    "case_id": "SEC-DIAG", "category": "security", "source_code": "value = 1\n",
    "expected_issue_type": "security", "expected_line": 1,
    "rationale": "fixture", "assumptions": "none", "reference_correction": None,
}


def _finding(evidence_ids, *, uncertain=False, abstained=False):
    return {
        "title": "Fixture finding", "issue_type": "security", "explanation": "Fixture",
        "recommended_fix": "Review the code", "line_start": 1,
        "evidence_ids": evidence_ids, "uncertain": uncertain, "abstained": abstained,
    }


class Provider:
    model = "offline-fixture"
    temperature = 0
    structured_output_metadata = {"mode": "fixture", "strict": False,
                                  "schema_name": None, "schema_version": "review-v1"}

    def __init__(self, response):
        self.response = response
        self.contexts = []

    def analyze_code(self, code, context):
        self.contexts.append(context)
        return self.response


def _evidence(text="Relevant style guide text", *, identifier="ev-1", metadata=None):
    return RetrievedEvidence(
        evidence_id=identifier, source_family="style_rule", source_url="https://example.test/doc?q=secret",
        rule_identifier="R-1", text=text, metadata=metadata or {"document_id": "doc-9", "similarity": 0.8},
        distance=0.2,
    )


def test_retrieval_diagnostics_capture_provenance_rank_scores_length_and_model_passage():
    evidence = [_evidence("first chunk"), _evidence("second chunk", identifier="ev-2")]
    provider = Provider(json.dumps({"summary": "Observed", "findings": [_finding(["ev-1"])]}))
    report = run_evaluation(provider=provider, mode="offline-test", cases=[CASE], max_cases=1,
                            rag_enabled=True, retriever=lambda _code: evidence)
    record = report["case_results"][0]
    first = record["retrieval_diagnostics"][0]
    assert first["chunk_id"] == "ev-1"
    assert first["retrieval_rank"] == 1
    assert first["source_category"] == "style_static"
    assert first["source_identifiers"] == {"document_id": "doc-9", "rule_identifier": "R-1"}
    assert first["distance"] == 0.2 and first["similarity"] == 0.8
    assert first["character_length"] == len("first chunk")
    assert first["token_count_estimate"] == 2
    assert first["passed_to_model"] is True
    assert "?q=secret" not in first["source_url"]
    assert provider.contexts[0] and "ev-1" in provider.contexts[0][0]


def test_previews_are_bounded_sanitized_or_disabled(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test_secret_123456")
    text = "prefix gsk_test_secret_123456 password=hunter2 " + "x" * 500
    evidence = _evidence(text)
    provider = Provider(json.dumps({"summary": "ok", "findings": []}))
    with_preview = run_evaluation(provider=provider, mode="offline-test", cases=[CASE], max_cases=1,
                                  rag_enabled=True, retriever=lambda _code: [evidence])
    preview = with_preview["case_results"][0]["retrieval_diagnostics"][0]["text_preview"]
    assert len(preview) <= 240
    assert "gsk_test_secret" not in preview and "hunter2" not in preview

    without = run_evaluation(provider=Provider(json.dumps({"summary": "ok", "findings": []})),
                             mode="offline-test", cases=[CASE], max_cases=1, rag_enabled=True,
                             retriever=lambda _code: [evidence], include_retrieval_text_previews=False)
    assert "text_preview" not in without["case_results"][0]["retrieval_diagnostics"][0]


def test_invalid_unknown_evidence_reference_survives_semantic_validation_rejection():
    provider = Provider(json.dumps({"summary": "unsupported", "findings": [_finding(["not-retrieved"])]}))
    report = run_evaluation(provider=provider, mode="offline-test", cases=[CASE], max_cases=1,
                            rag_enabled=True, retriever=lambda _code: [_evidence()])
    record = report["case_results"][0]
    assert record["schema_valid"] is False
    assert record["findings"] == []
    assert record["evidence_reference_diagnostics"][0]["reference_status"] == "unknown_reference"
    assert record["evidence_reference_diagnostics"][0]["reference_outcomes"] == [
        {"evidence_id": "not-retrieved", "outcome": "unknown_reference"}
    ]


def test_evidence_reference_outcomes_include_uncertain_abstained_and_missing():
    response = json.dumps({"summary": "Observed", "findings": [
        _finding([], uncertain=True), _finding([], abstained=True), _finding([]),
        _finding(["ev-1"]),
    ]})
    report = run_evaluation(provider=Provider(response), mode="offline-test", cases=[CASE], max_cases=1,
                            rag_enabled=True, retriever=lambda _code: [_evidence()])
    outcomes = [item["reference_status"] for item in report["case_results"][0]["evidence_reference_diagnostics"]]
    assert outcomes == ["uncited_but_explicitly_uncertain", "abstained", "missing_reference", "valid_reference"]


def test_timing_fields_are_separate_and_diagnostics_survive_checkpoint_resume(tmp_path):
    checkpoint = tmp_path / "diagnostics.checkpoint.json"
    evidence = _evidence()
    provider = Provider(json.dumps({"summary": "Observed", "findings": [_finding(["ev-1"])]}))
    first = run_evaluation(provider=provider, mode="offline-test", cases=[CASE], max_cases=1,
                           rag_enabled=True, retriever=lambda _code: [evidence], checkpoint_path=checkpoint)
    record = first["case_results"][0]
    assert set(record["timings"]) == {
        "request_pacing_seconds", "retrieval_seconds", "provider_inference_seconds",
        "validation_seconds", "total_case_duration_seconds",
    }
    assert record["timings"]["retrieval_seconds"] >= 0
    assert record["timings"]["provider_inference_seconds"] >= 0
    saved = json.loads(checkpoint.read_text())["case_results"][CASE["case_id"]]["result"]
    assert saved["retrieval_diagnostics"] == record["retrieval_diagnostics"]

    resumed = run_evaluation(provider=Provider("unused"), mode="offline-test", cases=[CASE], max_cases=1,
                             rag_enabled=True, retriever=lambda _code: [], checkpoint_path=checkpoint,
                             resume=True)
    assert resumed["case_results"][0]["retrieval_diagnostics"] == record["retrieval_diagnostics"]


def test_live_provider_pacing_and_inference_are_measured_separately():
    review = json.dumps({"summary": "Observed", "findings": [_finding([], uncertain=True)]})

    class FixProvider(Provider):
        def generate_fix(self, code, finding, context):
            return json.dumps({"finding_ref": finding.finding_id, "corrected_code": code,
                               "explanation": "No source change in this timing fixture.",
                               "assumptions": [], "evidence_ids": []})

    ticks = [100.0]
    sleeps = []

    def sleeper(seconds):
        sleeps.append(seconds)
        ticks[0] += seconds

    report = run_evaluation(provider=FixProvider(review), mode="groq", cases=[CASE], max_cases=1,
                            include_fixes=True, request_interval_seconds=3,
                            clock=lambda: ticks[0], sleeper=sleeper)
    timings = report["case_results"][0]["timings"]
    assert sleeps == [3.0]
    assert timings["request_pacing_seconds"] == 3.0
    assert timings["provider_inference_seconds"] >= 0
    assert timings["request_pacing_seconds"] != timings["provider_inference_seconds"]


def test_checkpoint_keeps_retrieval_diagnostics_for_completed_and_interrupted_cases(tmp_path):
    checkpoint = tmp_path / "interrupted.checkpoint.json"
    cases = [dict(CASE, case_id="FIRST", source_code="value = 1\n"),
             dict(CASE, case_id="SECOND", source_code="value = 2\n")]
    response = json.dumps({"summary": "No issues", "findings": []})

    class InterruptSecond(Provider):
        def analyze_code(self, code, context):
            if code == cases[1]["source_code"]:
                raise KeyboardInterrupt()
            return response

    retrieve = lambda _code: [_evidence()]
    first = run_evaluation(provider=InterruptSecond(response), mode="offline-test", cases=cases,
                           max_cases=2, rag_enabled=True, retriever=retrieve,
                           checkpoint_path=checkpoint)
    assert first["interrupted"] is True
    stored = json.loads(checkpoint.read_text())["case_results"]
    assert stored["FIRST"]["result"]["retrieval_diagnostics"][0]["chunk_id"] == "ev-1"
    assert stored["SECOND"]["result"]["retrieval_diagnostics"][0]["chunk_id"] == "ev-1"

    class ResumeProvider(Provider):
        def __init__(self, response):
            super().__init__(response)
            self.calls = 0

        def analyze_code(self, code, context):
            self.calls += 1
            return response

    resumed_provider = ResumeProvider(response)
    resumed = run_evaluation(provider=resumed_provider, mode="offline-test", cases=cases,
                             max_cases=2, rag_enabled=True, retriever=retrieve,
                             checkpoint_path=checkpoint, resume=True)
    assert resumed_provider.calls == 1
    assert resumed["case_results"][0]["retrieval_diagnostics"][0]["chunk_id"] == "ev-1"
