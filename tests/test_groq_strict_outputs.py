import json
from types import SimpleNamespace

import pytest

from llm import groq_client
from fix_generation import groq_fix_response_format
from review_schema import (
    REVIEW_SCHEMA_VERSION,
    RetrievedEvidence,
    groq_review_response_format,
    parse_review_response,
    review_json_schema,
)


EVIDENCE = RetrievedEvidence(
    evidence_id="ev-1", source_family="test", source_url=None,
    rule_identifier=None, text="Use parameterized queries.",
)


def _finding(**updates):
    value = {
        "title": "Potential injection",
        "issue_type": "security",
        "explanation": "Untrusted text is used in a query.",
        "affected_file": None,
        "line_start": 2,
        "line_end": 2,
        "recommended_fix": "Use parameters.",
        "corrected_code": None,
        "evidence_ids": ["ev-1"],
        "uncertain": False,
        "abstained": False,
        "risk_priority": None,
        "verification_status": "not_checked",
    }
    value.update(updates)
    return value


def _review(finding):
    return json.dumps({"summary": "Review summary", "findings": [finding]})


def test_valid_cited_finding_passes_semantic_validation():
    parsed = parse_review_response(_review(_finding()), [EVIDENCE])
    assert parsed.valid
    assert parsed.findings[0].evidence_ids == ("ev-1",)


def test_valid_uncited_uncertain_finding_passes():
    parsed = parse_review_response(
        _review(_finding(evidence_ids=[], uncertain=True)), []
    )
    assert parsed.valid
    assert parsed.findings[0].uncertain is True


def test_valid_uncited_abstained_finding_passes():
    parsed = parse_review_response(
        _review(_finding(evidence_ids=[], abstained=True)), []
    )
    assert parsed.valid
    assert parsed.findings[0].abstained is True


def test_uncited_confident_finding_remains_invalid():
    parsed = parse_review_response(
        _review(_finding(evidence_ids=[], uncertain=False, abstained=False)), []
    )
    assert not parsed.valid
    assert parsed.processing_errors == (
        "invalid_finding: finding_0: uncited_finding_must_be_uncertain_or_abstained",
    )


def test_unknown_evidence_id_remains_invalid():
    parsed = parse_review_response(_review(_finding(evidence_ids=["unknown"])), [EVIDENCE])
    assert not parsed.valid
    assert "unknown_evidence_id" in parsed.processing_errors[0]


@pytest.mark.parametrize("score", [True, "0.9", -0.01, 1.01, float("nan"), float("inf")])
def test_model_confidence_rejects_malformed_or_out_of_range_values(score):
    parsed = parse_review_response(_review(_finding(model_confidence_estimate=score)), [EVIDENCE])
    assert not parsed.valid
    assert "model_confidence_estimate_must_be_finite_number" in parsed.processing_errors[0]


def test_model_confidence_accepts_nullable_finite_score():
    parsed = parse_review_response(_review(_finding(model_confidence_estimate=0.83)), [EVIDENCE])
    assert parsed.valid
    assert parsed.findings[0].model_confidence_estimate == 0.83
    missing = parse_review_response(_review(_finding(model_confidence_estimate=None)), [EVIDENCE])
    assert missing.valid
    assert missing.findings[0].model_confidence_estimate is None


def test_missing_required_finding_field_remains_invalid():
    finding = _finding()
    finding.pop("title")
    parsed = parse_review_response(_review(finding), [EVIDENCE])
    assert not parsed.valid
    assert "required_text_field_missing_or_invalid" in parsed.processing_errors[0]


def test_invalid_finding_field_type_remains_invalid():
    parsed = parse_review_response(_review(_finding(uncertain="true")), [EVIDENCE])
    assert not parsed.valid
    assert "uncertainty_flags_must_be_boolean" in parsed.processing_errors[0]


def test_strict_schema_covers_all_parser_finding_fields_and_requires_them():
    schema = review_json_schema()
    finding_schema = schema["properties"]["findings"]["items"]
    assert set(finding_schema["properties"]) == set(finding_schema["required"])
    assert {"evidence_ids", "uncertain", "abstained"}.issubset(finding_schema["required"])
    assert schema["required"] == ["summary", "findings"]
    assert finding_schema["additionalProperties"] is False
    score_schema = finding_schema["properties"]["model_confidence_estimate"]
    assert {"type": "number", "minimum": 0, "maximum": 1} in score_schema["anyOf"]
    assert {"type": "null"} in score_schema["anyOf"]
    assert schema["additionalProperties"] is False
    assert groq_review_response_format() == {
        "type": "json_schema",
        "json_schema": {"name": "code_review", "strict": True, "schema": schema},
    }
    assert REVIEW_SCHEMA_VERSION == "review-v2"


def test_prompt_uncertainty_rule_and_example_are_consistent():
    from review_schema import REVIEW_OUTPUT_INSTRUCTIONS

    normalized = " ".join(REVIEW_OUTPUT_INSTRUCTIONS.split())
    assert "If no valid retrieved evidence supports a finding" in normalized
    assert ('"evidence_ids": [],\n      "model_confidence_estimate": null,\n'
            '      "uncertain": true') in REVIEW_OUTPUT_INSTRUCTIONS
    assert "subjective, uncalibrated estimate" in normalized
    assert "not a calibrated probability" in normalized


def test_groq_provider_sends_strict_response_format(monkeypatch):
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"summary":"No issues.","findings":[]}'))])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(groq_client, "Groq", lambda **_kwargs: fake_client)
    provider = groq_client.GroqLLM(model="openai/gpt-oss-20b")

    output = provider.analyze_code("x = 1", [])

    assert json.loads(output) == {"summary": "No issues.", "findings": []}
    assert calls[0]["response_format"] == groq_review_response_format()
    assert provider.structured_output_metadata == {
        "mode": "json_schema", "strict": True,
        "schema_name": "code_review", "schema_version": REVIEW_SCHEMA_VERSION,
    }


def test_groq_independently_assesses_static_formatting_with_strict_schema(monkeypatch):
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content='{"assessment":"supports","model_confidence_estimate":0.91}'))])

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(groq_client, "Groq", lambda **_kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    ))
    provider = groq_client.GroqLLM(model="openai/gpt-oss-20b")
    finding = {"rule_id": "PYFMT001", "issue_type": "formatting",
               "title": "Missing spaces", "explanation": "Spaces missing.", "line_start": 2}

    assessment = provider.assess_static_finding_confidence("value=1\n", finding)

    assert assessment == {
        "assessment": "supports",
        "assessment_source": "groq_independent_static_assessment",
        "model_confidence_estimate": 0.91,
    }
    assert calls[0]["response_format"]["json_schema"]["name"] == "static_finding_assessment"
    assert "untrusted code" in calls[0]["messages"][1]["content"]


@pytest.mark.parametrize("response", [
    '{"assessment":"uncertain","model_confidence_estimate":0.99}',
    '{"assessment":"supports","model_confidence_estimate":null}',
    '{"assessment":"supports","model_confidence_estimate":1.2}',
    '{"assessment":"supports","model_confidence_estimate":true}',
])
def test_groq_static_assessment_rejects_missing_or_invalid_confidence(monkeypatch, response):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(groq_client, "Groq", lambda **_kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_args: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=response))]
        )))
    ))
    provider = groq_client.GroqLLM(model="openai/gpt-oss-20b")
    assert provider.assess_static_finding_confidence(
        "value=1\n", {"rule_id": "PYFMT001", "issue_type": "formatting"}
    ) is None


def test_groq_fix_uses_strict_schema_pinned_to_requested_finding(monkeypatch):
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({
                    "finding_ref": "finding-1", "corrected_code": "x = 2\n",
                    "explanation": "Correct the value.", "assumptions": [], "evidence_ids": [],
                })))])

    parsed = parse_review_response(_review(_finding()), [EVIDENCE])
    finding = parsed.findings[0]
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(groq_client, "Groq", lambda **_kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())))
    provider = groq_client.GroqLLM(model="openai/gpt-oss-20b")

    result = provider.generate_fix("x = 1\n", finding, [])

    assert json.loads(result)["finding_ref"] == finding.finding_id
    assert calls[0]["response_format"] == groq_fix_response_format(finding)
    schema = calls[0]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["finding_ref"]["enum"] == [finding.finding_id]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(schema["required"])


def test_unsupported_model_fails_explicitly_without_unconstrained_fallback(monkeypatch):
    calls = []
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: calls.append(kwargs))))
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(groq_client, "Groq", lambda **_kwargs: fake_client)
    provider = groq_client.GroqLLM(model="unknown/model")

    with pytest.raises(groq_client.UnsupportedStructuredOutputModelError,
                       match="Strict review JSON Schema output is unsupported"):
        provider.analyze_code("x = 1", [])
    assert calls == []


def test_groq_raw_response_metadata_captures_quota_headers_usage_and_sdk_retries(monkeypatch):
    completion = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"summary":"ok","findings":[]}'))],
        usage=SimpleNamespace(prompt_tokens=321, completion_tokens=45, total_tokens=366),
    )

    class Raw:
        headers = {"x-ratelimit-remaining-requests": "900",
                   "x-ratelimit-remaining-tokens": "6000",
                   "retry-after": "2", "authorization": "must-not-be-recorded"}
        retries_taken = 1

        def parse(self):
            return completion

    class Completions:
        with_raw_response = SimpleNamespace(create=lambda **_kwargs: Raw())

    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(groq_client, "Groq", lambda **_kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())))
    provider = groq_client.GroqLLM(model="openai/gpt-oss-20b")

    provider.analyze_code("x = 1", [])

    assert provider.last_response_metadata["usage"] == {
        "prompt_tokens": 321, "completion_tokens": 45, "total_tokens": 366,
    }
    assert provider.last_response_metadata["headers"]["x-ratelimit-remaining-requests"] == "900"
    assert "authorization" not in provider.last_response_metadata["headers"]
    assert provider.last_response_metadata["sdk_retries"] == 1
