import json

from mcp_server import orchestrator
from rag import retrieve
from review_schema import RetrievedEvidence, parse_review_response


class FakeCollection:
    def __init__(self, result):
        self.result = result
        self.query_args = None

    def count(self):
        return 10

    def query(self, **kwargs):
        self.query_args = kwargs
        return self.result


def _result(ids, documents, metadatas, distances):
    return {
        "ids": [ids],
        "documents": [documents],
        "metadatas": [metadatas],
        "distances": [distances],
    }


def test_structured_retrieval_extracts_chroma_evidence(monkeypatch):
    collection = FakeCollection(
        _result(
            ["cwe-89-record"],
            ["Use parameterized queries."],
            [{"source": "owasp_cwe", "cwe_id": "CWE-89", "language": "python", "source_url": "https://example.test/cwe-89"}],
            [0.14],
        )
    )
    monkeypatch.setattr(retrieve, "_get_collection", lambda: collection)

    evidence = retrieve.retrieve_evidence("db.execute(query)")

    assert len(evidence) == 1
    item = evidence[0]
    assert item.evidence_id == "cwe-89-record"
    assert item.source_family == "owasp_cwe"
    assert item.source_url == "https://example.test/cwe-89"
    assert item.rule_identifier == "CWE-89"
    assert item.language == "python"
    assert item.distance == 0.14
    assert "cwe-89-record" in item.to_prompt_text()
    assert collection.query_args["n_results"] == retrieve.DEFAULT_K


def test_structured_retrieval_does_not_fabricate_missing_metadata(monkeypatch):
    collection = FakeCollection(_result(["record-1"], ["Reference text."], [None], [None]))
    monkeypatch.setattr(retrieve, "_get_collection", lambda: collection)

    item = retrieve.retrieve_evidence("x = 1")[0]

    assert item.evidence_id == "record-1"
    assert item.source_family is None
    assert item.source_url is None
    assert item.rule_identifier is None
    assert item.language is None
    assert item.metadata == {}
    assert item.distance is None


def test_legacy_retrieval_api_still_returns_text(monkeypatch):
    evidence = RetrievedEvidence(
        evidence_id="r1",
        source_family="style_static_rules",
        source_url=None,
        rule_identifier="B101",
        text="Avoid assert statements in production code.",
    )
    monkeypatch.setattr(retrieve, "retrieve_evidence", lambda _code: [evidence])

    assert retrieve.retrieve_context("assert x") == [evidence.text]


def test_valid_review_response_cites_supplied_evidence():
    evidence = RetrievedEvidence(
        evidence_id="rule-17",
        source_family="style_static_rules",
        source_url=None,
        rule_identifier="B101",
        text="Avoid assert statements in production code.",
    )
    raw = json.dumps(
        {
            "summary": "One issue found.",
            "findings": [
                {
                    "title": "Assertion used for runtime validation",
                    "issue_type": "bug",
                    "explanation": "Assertions can be disabled by the interpreter.",
                    "affected_file": "app.py",
                    "line_start": 4,
                    "line_end": 4,
                    "recommended_fix": "Use an explicit conditional and exception.",
                    "corrected_code": "if not value:\n    raise ValueError()",
                    "evidence_ids": ["rule-17"],
                }
            ],
        }
    )

    response = parse_review_response(raw, [evidence])

    assert response.valid is True
    finding = response.findings[0]
    assert finding.evidence_ids == ("rule-17",)
    assert finding.verification_status == "not_checked"
    assert finding.risk_priority is None
    assert response.raw_response == raw


def test_unknown_evidence_id_is_rejected_without_findings():
    raw = json.dumps(
        {
            "summary": "One issue found.",
            "findings": [
                {
                    "title": "Potential issue",
                    "issue_type": "bug",
                    "explanation": "This needs review.",
                    "recommended_fix": "Validate the value.",
                    "evidence_ids": ["not-retrieved"],
                }
            ],
        }
    )

    response = parse_review_response(raw, [])

    assert response.valid is False
    assert response.findings == ()
    assert "unknown_evidence_id" in response.processing_errors[0]
    assert response.raw_response == raw


def test_malformed_model_output_is_invalid_and_preserved():
    response = parse_review_response("Here is my review: this may be unsafe.")

    assert response.valid is False
    assert response.findings == ()
    assert response.processing_errors == ("invalid_json: expected_one_json_object",)
    assert response.raw_response == "Here is my review: this may be unsafe."


def test_pipeline_uses_structured_retrieval_and_returns_validated_response():
    evidence = RetrievedEvidence(
        evidence_id="cwe-89-record",
        source_family="owasp_cwe",
        source_url=None,
        rule_identifier="CWE-89",
        text="Use parameterized queries.",
    )
    raw = json.dumps(
        {
            "summary": "Potential SQL injection.",
            "findings": [
                {
                    "title": "Unparameterized query",
                    "issue_type": "security",
                    "explanation": "User-controlled input is interpolated into SQL.",
                    "recommended_fix": "Use a parameterized query.",
                    "evidence_ids": ["cwe-89-record"],
                }
            ],
        }
    )

    class FakeLLM:
        def analyze_code(self, code, context):
            assert "evidence_id: cwe-89-record" in context[0]
            return raw

    result = orchestrator.run_review_pipeline(
        code="query = f'SELECT {user_value}'",
        llm=FakeLLM(),
        retriever=lambda _code: [evidence],
        notify_desktop=False,
    )

    assert result.review_response.valid is True
    assert result.review_response.evidence == (evidence,)
    assert result.review_response.findings[0].evidence_ids == ("cwe-89-record",)
    assert result.review_response.findings[0].verification_status == "not_checked"


def test_provider_prompts_share_contract_and_mark_references_untrusted():
    from llm.groq_client import GroqLLM
    from llm.qwen_client import QwenLLM
    from review_schema import REVIEW_OUTPUT_INSTRUCTIONS

    evidence_text = "evidence_id: rule-17\nsource_family: style_static_rules\ntext:\nUse explicit checks."
    groq_prompt = GroqLLM._build_prompt("if value: pass", [evidence_text])
    assert REVIEW_OUTPUT_INSTRUCTIONS in groq_prompt
    assert "UNTRUSTED RETRIEVED REFERENCE DATA" in groq_prompt
    assert "Do not predict severity." in groq_prompt

    class FakeTokenizer:
        def apply_chat_template(self, messages, **_kwargs):
            return list(range(sum(len(message["content"].split()) for message in messages)))

    qwen = object.__new__(QwenLLM)
    qwen.tokenizer = FakeTokenizer()
    messages = qwen._build_messages("if value: pass", [evidence_text])
    assert REVIEW_OUTPUT_INSTRUCTIONS in messages[0]["content"]
    assert "<untrusted_reference>" in messages[1]["content"]
    assert "evidence_id: rule-17" in messages[1]["content"]


def test_uncertain_finding_without_evidence_is_allowed():
    raw = json.dumps(
        {
            "summary": "One uncertain finding.",
            "findings": [
                {
                    "title": "Potential issue",
                    "issue_type": "bug",
                    "explanation": "This pattern may be unsafe.",
                    "recommended_fix": "Review the input handling.",
                    "evidence_ids": [],
                    "uncertain": True,
                }
            ],
        }
    )

    response = parse_review_response(raw)

    assert response.valid is True
    assert response.findings[0].evidence_ids == ()
    assert response.findings[0].uncertain is True
    assert response.findings[0].verification_status == "not_checked"


def test_pipeline_rejects_unknown_evidence_ids():
    raw = json.dumps(
        {
            "summary": "One issue found.",
            "findings": [
                {
                    "title": "Potential issue",
                    "issue_type": "bug",
                    "explanation": "This needs review.",
                    "recommended_fix": "Validate the value.",
                    "evidence_ids": ["invented-id"],
                }
            ],
        }
    )

    class FakeLLM:
        def analyze_code(self, code, context):
            return raw

    result = orchestrator.run_review_pipeline(
        code="x = unchecked()",
        llm=FakeLLM(),
        retriever=lambda _code: [],
        notify_desktop=False,
    )

    assert result.review_response.valid is False
    assert result.review_response.findings == ()
    assert "unknown_evidence_id" in result.review_response.processing_errors[0]


def test_mcp_response_includes_evidence_for_referenced_ids(monkeypatch):
    from mcp_server import orchestrator, server

    evidence = RetrievedEvidence(
        evidence_id="rule-24",
        source_family="style_static_rules",
        source_url=None,
        rule_identifier="B101",
        text="Use explicit checks.",
    )
    raw = json.dumps(
        {
            "summary": "One issue found.",
            "findings": [
                {
                    "title": "Assertion used for validation",
                    "issue_type": "bug",
                    "explanation": "Assertions may be disabled.",
                    "recommended_fix": "Use an explicit conditional.",
                    "evidence_ids": ["rule-24"],
                }
            ],
        }
    )

    class FakeLLM:
        def analyze_code(self, code, context):
            return raw

    original_pipeline = orchestrator.run_review_pipeline
    monkeypatch.setattr(
        server,
        "run_review_pipeline",
        lambda **kwargs: original_pipeline(
            **kwargs,
            llm=FakeLLM(),
            retriever=lambda _code: [evidence],
            notify_desktop=False,
        ),
    )

    response = json.loads(server.review_code("assert value", file_path="x.py"))

    assert response["valid"] is True
    assert response["findings"][0]["evidence_ids"] == ["rule-24"]
    assert response["evidence"][0]["evidence_id"] == "rule-24"
