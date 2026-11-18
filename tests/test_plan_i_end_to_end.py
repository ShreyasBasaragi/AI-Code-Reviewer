import json

import pytest
from fastapi.testclient import TestClient

from mcp_server.http_api import ReviewFixService, create_app
from mcp_server.state import LifecycleState
from review_schema import RetrievedEvidence


def response(findings=(), summary="Offline integration fixture"):
    return json.dumps({"summary": summary, "findings": list(findings)})


class Provider:
    def __init__(self, review=None, fix=None, error=None):
        self.review = review if review is not None else response()
        self.fix = fix
        self.error = error
        self.contexts = []
        self.review_calls = 0
        self.fix_calls = 0

    def analyze_code(self, code, context):
        self.review_calls += 1
        self.contexts.append(context)
        if self.error:
            raise self.error
        return self.review

    def generate_fix(self, code, finding, context):
        self.fix_calls += 1
        return self.fix


def make_client(provider, retriever=None):
    service = ReviewFixService(llm=provider, retriever=retriever, state=LifecycleState())
    return TestClient(create_app(service)), service


def test_manual_review_runs_retrieval_validation_static_merge_and_returns_timings():
    evidence = RetrievedEvidence(evidence_id="evidence-1", source_family="fixture", source_url=None,
                                 rule_identifier="R-1", text="Treat dynamic evaluation as unsafe.")
    llm_finding = {"title": "Dynamic execution", "issue_type": "security",
                   "explanation": "eval() executes dynamic code.", "recommended_fix": "Use a safe parser.",
                   "line_start": 2, "evidence_ids": ["evidence-1"], "uncertain": False, "abstained": False}
    provider = Provider(response([llm_finding]))
    retrievals = []
    client, _ = make_client(provider, lambda code: retrievals.append(code) or [evidence])
    res = client.post("/api/reviews", json={"code": "def f(value):\n    return eval(value)\n"})
    assert res.status_code == 201
    payload = res.json()
    findings = payload["review"]["findings"]
    assert len(retrievals) == 1 and len(provider.contexts) == 1
    assert "evidence-1" in provider.contexts[0][0]
    assert len(findings) == 1
    assert findings[0]["origin"] == "llm" and findings[0]["also_detected_by"] == ["static"]
    assert findings[0]["rule_id"] == "PYSEC001"
    assert set(payload["timings"]) >= {"request_validation_seconds", "rag_retrieval_seconds",
                                       "model_generation_seconds", "response_validation_seconds",
                                       "static_analysis_seconds", "total_review_seconds", "total_request_seconds"}
    assert all(value >= 0 for value in payload["timings"].values())


@pytest.mark.parametrize("code,expected_static,static_status", [
    ("def f():\n    return 1\n", 0, "passed"),
    ("value = eval(source)\nimport pickle\ndata = pickle.loads(blob)\n", 2, "failed"),
    ("def broken(:\n    pass\n", 0, "errored"),
])
def test_manual_review_clean_multiple_vulnerabilities_and_syntax_error(code, expected_static, static_status):
    client, _ = make_client(Provider())
    res = client.post("/api/reviews", json={"code": code})
    assert res.status_code == 201
    review = res.json()["review"]
    assert review["static_analysis_status"] == static_status
    assert sum(item["origin"] == "static" for item in review["findings"]) == expected_static


def test_invalid_request_is_rejected_before_provider_invocation():
    provider = Provider()
    client, _ = make_client(provider)
    assert client.post("/api/reviews", json={"code": ""}).status_code == 422
    assert client.post("/api/reviews", json={"code": "x = 1", "extra": True}).status_code == 422
    assert provider.review_calls == 0


def test_invalid_evidence_reference_and_malformed_model_output_are_rejected():
    evidence = RetrievedEvidence(evidence_id="available", source_family="fixture", source_url=None,
                                 rule_identifier=None, text="content")
    bad_reference = {"title": "Finding", "issue_type": "security", "explanation": "Unsupported",
                     "recommended_fix": "Check", "line_start": 1, "evidence_ids": ["unknown"],
                     "uncertain": False, "abstained": False}
    client, _ = make_client(Provider(response([bad_reference])), lambda _code: [evidence])
    assert client.post("/api/reviews", json={"code": "x = 1"}).status_code == 502
    malformed, _ = make_client(Provider("not json"))
    assert malformed.post("/api/reviews", json={"code": "x = 1"}).status_code == 502


def test_provider_failure_is_sanitized_and_retrieval_failure_is_nonfatal():
    failing_provider, _ = make_client(Provider(error=RuntimeError("private token gsk_secret")))
    failed = failing_provider.post("/api/reviews", json={"code": "x = 1"})
    assert failed.status_code == 502 and "gsk_secret" not in failed.text
    provider = Provider()
    client, _ = make_client(provider, lambda _code: (_ for _ in ()).throw(TimeoutError("offline fixture")))
    accepted = client.post("/api/reviews", json={"code": "x = 1"})
    assert accepted.status_code == 201
    assert provider.contexts == [[]]


def test_api_fix_path_requires_verify_then_explicit_acceptance_and_exports_without_applying():
    corrected = "value = 1\n"
    class FixProvider(Provider):
        def generate_fix(self, code, finding, context):
            self.fix_calls += 1
            return json.dumps({"finding_ref": finding.finding_id, "corrected_code": corrected,
                               "explanation": "Remove dynamic code execution.", "assumptions": [], "evidence_ids": []})

    provider = FixProvider(response())
    client, _ = make_client(provider)
    review = client.post("/api/reviews", json={"code": "value = eval('1')\n"})
    finding = review.json()["review"]["findings"][0]
    candidate = client.post(f"/api/reviews/{review.json()['review_id']}/findings/{finding['finding_id']}/fixes", json={})
    assert candidate.status_code == 201
    candidate_id = candidate.json()["candidate_id"]
    blocked = client.post(f"/api/fixes/{candidate_id}/accept")
    assert blocked.status_code == 200 and blocked.json()["outcome"] == "blocked"
    verified = client.post(f"/api/fixes/{candidate_id}/verify")
    assert verified.status_code == 200 and verified.json()["syntax_check"]["status"] == "passed"
    assert verified.json()["static_analysis_status"] == "passed"
    accepted = client.post(f"/api/fixes/{candidate_id}/accept")
    assert accepted.status_code == 200 and accepted.json()["candidate"]["approval_status"] == "accepted"
    exported = client.get(f"/api/fixes/{candidate_id}/export")
    assert exported.status_code == 200 and exported.json()["corrected_code"] == corrected
