import json
from dataclasses import replace

import pytest

from fix_generation import parse_fix_response
from fix_lifecycle import record_verification
from fix_verification import verify_fix_candidate
from mcp_server import orchestrator
from review_schema import ReviewFinding, parse_review_response
from static_analysis import analyze_python


@pytest.mark.parametrize(
    ("source", "rule_id"),
    [
        ("value = eval(user_input)\n", "PYSEC001"),
        ("exec(source)\n", "PYSEC001"),
        ("import pickle\npickle.loads(blob)\n", "PYSEC002"),
        ("import yaml\nyaml.load(data, Loader=yaml.UnsafeLoader)\n", "PYSEC003"),
        ("import subprocess\nsubprocess.run(cmd, shell=True)\n", "PYSEC004"),
        ("import os\nos.system(command)\n", "PYSEC005"),
    ],
)
def test_static_rules_find_identifiable_unsafe_calls(source, rule_id):
    result = analyze_python(source, file_path="example.py")
    assert result.status == "failed"
    assert [finding.rule_id for finding in result.findings] == [rule_id]
    assert result.findings[0].affected_file == "example.py"
    assert result.findings[0].line_start > 0
    assert result.findings[0].severity == "high"
    assert result.findings[0].origin == "static"


def test_safe_code_has_no_findings():
    result = analyze_python(
        "import yaml\nimport subprocess\nyaml.safe_load(data)\nyaml.load(data, Loader=yaml.SafeLoader)\n"
        "subprocess.run([program, arg], shell=False)\n"
    )
    assert result.status == "passed"
    assert result.findings == ()


def test_malformed_python_reports_location_without_findings():
    result = analyze_python("def broken(:\n    pass\n")
    assert result.status == "errored"
    assert result.error_type == "SyntaxError"
    assert result.error_line == 1
    assert result.findings == ()


def test_static_finding_ids_are_repeatable():
    source = "def run(value):\n    return eval(value)\n"
    first = analyze_python(source, file_path="sample.py")
    second = analyze_python(source, file_path="sample.py")
    assert first.findings == second.findings
    assert first.findings[0].finding_id.startswith("static-")


class FakeReviewProvider:
    def __init__(self):
        self.fix_calls = []

    def analyze_code(self, code, context):
        return json.dumps({
            "summary": "Review complete.",
            "findings": [{
                "title": "Input handling needs review",
                "issue_type": "security",
                "explanation": "Review the user controlled input.",
                "recommended_fix": "Validate the input.",
                "evidence_ids": [],
                "uncertain": True,
            }],
        })

    def generate_fix(self, code, finding, context):
        self.fix_calls.append((code, finding, context))
        return json.dumps({
            "finding_ref": finding.finding_id,
            "corrected_code": "def run(value):\n    return value\n",
            "explanation": "Avoid dynamic evaluation.",
            "assumptions": [],
            "evidence_ids": [],
        })


def test_pipeline_merges_static_and_validated_llm_findings():
    result = orchestrator.run_review_pipeline(
        "def run(value):\n    return eval(value)\n",
        llm=FakeReviewProvider(),
        retriever=lambda _code: [],
        notify_desktop=False,
    )
    assert result.review_response.valid
    assert [item.origin for item in result.review_response.findings] == ["llm", "static"]
    static = result.review_response.findings[1]
    assert static.rule_id == "PYSEC001"
    assert static.evidence_ids == ()
    assert static.severity == "high"
    assert result.review_response.static_analysis_status == "failed"


def test_unique_same_line_overlap_is_deduplicated_with_both_origins_preserved():
    class OverlapProvider(FakeReviewProvider):
        def analyze_code(self, code, context):
            return json.dumps({
                "summary": "Review complete.",
                "findings": [{
                    "title": "eval can execute arbitrary code",
                    "issue_type": "security",
                    "explanation": "The eval call on line 2 is unsafe.",
                    "recommended_fix": "Avoid eval.",
                    "line_start": 2,
                    "line_end": 2,
                    "evidence_ids": [],
                    "uncertain": True,
                }],
            })

    code = "def run(value):\n    return eval(value)\n"
    response = orchestrator.run_review_pipeline(
        code, llm=OverlapProvider(), retriever=lambda _code: [], notify_desktop=False
    ).review_response
    assert len(response.findings) == 1
    assert response.findings[0].origin == "llm"
    assert response.findings[0].also_detected_by == ("static",)
    assert response.findings[0].rule_id == "PYSEC001"


def test_deduplicated_overlap_remains_eligible_for_validated_fix_generation():
    class OverlapProvider(FakeReviewProvider):
        def analyze_code(self, code, context):
            return json.dumps({
                "summary": "Review complete.",
                "findings": [{
                    "title": "eval can execute code", "issue_type": "security",
                    "explanation": "Avoid eval on line 2.", "recommended_fix": "Avoid eval.",
                    "line_start": 2, "line_end": 2, "evidence_ids": [], "uncertain": True,
                }],
            })

    provider = OverlapProvider()
    code = "def run(value):\n    return eval(value)\n"
    response = orchestrator.run_review_pipeline(
        code, llm=provider, retriever=lambda _code: [], notify_desktop=False
    ).review_response
    finding = response.findings[0]
    candidate = orchestrator.generate_fix_candidate(response, finding.finding_id, code, llm=provider)
    assert candidate.generation_status == "generated"


def test_static_findings_can_use_fix_workflow_but_are_recomputed():
    provider = FakeReviewProvider()
    code = "def run(value):\n    return eval(value)\n"
    response = orchestrator.run_review_pipeline(
        code, llm=provider, retriever=lambda _code: [], notify_desktop=False
    ).review_response
    static = next(item for item in response.findings if item.origin == "static")
    candidate = orchestrator.generate_fix_candidate(response, static.finding_id, code, llm=provider)
    assert candidate.generation_status == "generated"
    assert provider.fix_calls[0][1].rule_id == "PYSEC001"
    tampered = replace(response, findings=tuple(
        replace(item, explanation="tampered") if item.origin == "static" else item
        for item in response.findings
    ))
    denied = orchestrator.generate_fix_candidate(tampered, static.finding_id, code, llm=provider)
    assert denied.generation_status == "failed"
    assert "invalid_review_response" in denied.generation_errors[0]


def test_llm_evidence_id_validation_is_unchanged():
    parsed = parse_review_response(json.dumps({
        "summary": "Review",
        "findings": [{
            "title": "Issue", "issue_type": "bug", "explanation": "Details",
            "recommended_fix": "Fix", "evidence_ids": ["invented"],
        }],
    }))
    assert not parsed.valid
    assert "unknown_evidence_id" in parsed.processing_errors[0]


def test_merge_removes_only_exact_same_origin_rule_duplicates():
    item = ReviewFinding(
        title="Unsafe call", issue_type="security", explanation="Rule match",
        recommended_fix="Replace it.", finding_id="static-one", origin="static",
        rule_id="PYSEC001", severity="high",
    )
    merged = orchestrator._merge_review_findings((), (item, item))
    assert len(merged) == 1
    independent = replace(item, rule_id="PYSEC002")
    assert len(orchestrator._merge_review_findings((), (item, independent))) == 2
    same_line_calls = analyze_python("eval(first); eval(second)").findings
    converted = tuple(orchestrator._static_review_finding(hit) for hit in same_line_calls)
    assert len(orchestrator._merge_review_findings((), converted)) == 2


def _candidate(original, corrected):
    payload = json.dumps({
        "finding_ref": "finding-1", "corrected_code": corrected,
        "explanation": "Candidate change.", "assumptions": [], "evidence_ids": [],
    })
    return parse_fix_response(
        payload, finding_ref="finding-1", original_code=original, allowed_evidence_ids=[]
    )


def test_verification_fails_for_new_static_violation_and_preserves_baseline():
    original = "def old(value):\n    return eval(value)\n"
    corrected = (
        "def old(value):\n    return eval(value)\n"
        "def new(command):\n    import os\n    os.system(command)\n"
    )
    report = verify_fix_candidate(_candidate(original, corrected))
    assert report.static_analysis_status == "failed"
    assert report.verification_status == "static_analysis_failed"
    assert {item["rule_id"] for item in report.pre_existing_static_issues} == {"PYSEC001"}
    assert {item["rule_id"] for item in report.introduced_static_issues} == {"PYSEC005"}


def test_verification_passes_when_existing_issue_is_unchanged():
    source = "def run(value):\n    return eval(value)\n"
    report = verify_fix_candidate(_candidate(source, source + "\n# harmless comment\n"))
    assert report.static_analysis_status == "passed"
    assert report.pre_existing_static_issues[0]["rule_id"] == "PYSEC001"
    assert report.introduced_static_issues == ()


def test_candidate_code_is_never_executed(tmp_path):
    marker = tmp_path / "executed"
    source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
    report = verify_fix_candidate(_candidate("x = 1\n", source))
    assert report.static_analysis_status == "passed"
    assert not marker.exists()


def test_acceptance_and_export_require_passed_static_check():
    candidate = _candidate("x = 1\n", "value = eval(user_input)\n")
    candidate = record_verification(candidate, verify_fix_candidate(candidate))
    accepted = orchestrator.accept_fix(candidate)
    assert accepted.outcome == "blocked"
    assert accepted.approval_status == "pending"
    forged_accepted = replace(candidate, approval_status="accepted")
    with pytest.raises(ValueError, match="successful matching syntax and static"):
        orchestrator.export_fix(forged_accepted)
