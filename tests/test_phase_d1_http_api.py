import importlib
import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from mcp_server.http_api import ReviewFixService, create_app
from mcp_server.state import LifecycleState, PROCESS_STATE


class FakeRetriever:
    def __init__(self):
        self.calls = []

    def __call__(self, code):
        self.calls.append(code)
        return []


class FakeProvider:
    def __init__(self, *, review_error=None, fix_error=None):
        self.review_error = review_error
        self.fix_error = fix_error
        self.review_calls = []
        self.fix_calls = []

    def analyze_code(self, code, context):
        self.review_calls.append((code, context))
        if self.review_error:
            raise RuntimeError(self.review_error)
        return json.dumps(
            {
                "summary": "One issue found.",
                "findings": [
                    {
                        "title": "Return value is incorrect",
                        "issue_type": "bug",
                        "explanation": "The function returns the wrong value.",
                        "recommended_fix": "Return 2.",
                        "evidence_ids": [],
                        "uncertain": True,
                    }
                ],
            }
        )

    def generate_fix(self, code, finding, context):
        self.fix_calls.append((code, finding, context))
        if self.fix_error:
            raise RuntimeError(self.fix_error)
        return json.dumps(
            {
                "finding_ref": "finding-1",
                "corrected_code": "def value():\n    return 2\n",
                "explanation": "Return the expected value.",
                "assumptions": [],
                "evidence_ids": [],
            }
        )


def make_client(provider=None, retriever=None, state=None):
    lifecycle = ReviewFixService(
        llm=provider or FakeProvider(),
        retriever=retriever or FakeRetriever(),
        state=state or LifecycleState(),
    )
    return TestClient(create_app(lifecycle)), lifecycle


def complete_workflow(client):
    review_response = client.post(
        "/api/reviews", json={"code": "def value():\n    return 1\n"}
    )
    assert review_response.status_code == 201
    review_id = review_response.json()["review_id"]
    review = client.get(f"/api/reviews/{review_id}")
    assert review.status_code == 200
    candidate_response = client.post(
        f"/api/reviews/{review_id}/findings/finding-1/fixes", json={}
    )
    assert candidate_response.status_code == 201
    candidate = candidate_response.json()
    return review_id, candidate["candidate_id"]


def test_http_full_review_fix_verify_accept_export_workflow():
    provider = FakeProvider()
    retriever = FakeRetriever()
    client, _ = make_client(provider, retriever)
    _, candidate_id = complete_workflow(client)

    fetched = client.get(f"/api/fixes/{candidate_id}")
    assert fetched.status_code == 200
    assert fetched.json()["approval_status"] == "pending"
    verified = client.post(f"/api/fixes/{candidate_id}/verify")
    assert verified.json()["verification_status"] == "syntax_passed"
    accepted = client.post(f"/api/fixes/{candidate_id}/accept")
    assert accepted.json()["outcome"] == "accepted"
    exported = client.get(f"/api/fixes/{candidate_id}/export")
    assert exported.status_code == 200
    assert exported.json()["corrected_code"] == "def value():\n    return 2\n"
    assert exported.json()["filename"] == "candidate.py"
    assert provider.review_calls and provider.fix_calls
    assert retriever.calls == ["def value():\n    return 1\n"]


def test_first_and_subsequent_fix_requests_use_the_reviewed_finding_identifier():
    provider = FakeProvider()
    client, _ = make_client(provider)
    reviewed = client.post("/api/reviews", json={"code": "def value():\n    return 1\n"})
    assert reviewed.status_code == 201
    review_id = reviewed.json()["review_id"]
    finding = reviewed.json()["review"]["findings"][0]
    path = f"/api/reviews/{review_id}/findings/{finding['finding_id']}/fixes"

    first = client.post(path, json={"language": "python"})
    second = client.post(path, json={"language": "python"})

    assert first.status_code == second.status_code == 201
    assert first.json()["finding_ref"] == second.json()["finding_ref"] == finding["finding_id"]
    assert provider.fix_calls and len(provider.fix_calls) == 2


def test_rejected_fix_provider_output_is_not_misreported_as_client_validation():
    class InvalidOutputProvider(FakeProvider):
        def generate_fix(self, code, finding, context):
            self.fix_calls.append((code, finding, context))
            return "not json"

    provider = InvalidOutputProvider()
    client, _ = make_client(provider)
    reviewed = client.post("/api/reviews", json={"code": "def value():\n    return 1\n"})
    review_id = reviewed.json()["review_id"]
    finding_id = reviewed.json()["review"]["findings"][0]["finding_id"]

    response = client.post(
        f"/api/reviews/{review_id}/findings/{finding_id}/fixes", json={"language": "python"}
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "fix_provider_response_invalid"
    assert len(provider.fix_calls) == 1


def test_review_api_exposes_static_origin_and_rule_metadata():
    client, _ = make_client()
    response = client.post(
        "/api/reviews", json={"code": "def run(value):\n    return eval(value)\n"}
    )
    assert response.status_code == 201
    review = response.json()["review"]
    static = next(item for item in review["findings"] if item["origin"] == "static")
    assert static["rule_id"] == "PYSEC001"
    assert static["severity"] == "high"
    assert static["evidence_ids"] == []
    assert review["static_analysis_status"] == "failed"


def test_reject_workflow_and_export_is_blocked():
    client, _ = make_client()
    _, candidate_id = complete_workflow(client)
    rejected = client.post(f"/api/fixes/{candidate_id}/reject")
    assert rejected.json()["approval_status"] == "rejected"
    assert rejected.json()["outcome"] == "rejected"
    assert client.get(f"/api/fixes/{candidate_id}/export").status_code == 409


def test_unknown_review_and_candidate_handles_return_404():
    client, _ = make_client()
    assert client.get("/api/reviews/missing").status_code == 404
    assert client.get("/api/fixes/missing").status_code == 404
    assert client.post("/api/fixes/missing/verify").status_code == 404
    assert client.post("/api/fixes/missing/accept").status_code == 404


def test_expired_review_handle_returns_404():
    state = LifecycleState(limit=1)
    client, _ = make_client(state=state)
    first = client.post("/api/reviews", json={"code": "x = 1\n"}).json()["review_id"]
    client.post("/api/reviews", json={"code": "x = 2\n"})
    assert client.get(f"/api/reviews/{first}").status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        {"code": ""},
        {"code": "x = 1", "language": "javascript"},
        {"code": "x = 1", "unexpected": True},
    ],
)
def test_invalid_review_input_is_rejected(payload):
    client, _ = make_client()
    assert client.post("/api/reviews", json=payload).status_code == 422


def test_code_utf8_byte_limit_boundary_is_enforced():
    client, _ = make_client()
    below_limit = client.post("/api/reviews", json={"code": "€" * 174_762})
    assert below_limit.status_code == 201  # 524,286 UTF-8 bytes
    over_limit = client.post("/api/reviews", json={"code": "€" * 174_763})
    assert over_limit.status_code == 413  # 524,289 UTF-8 bytes


def test_provider_failures_are_sanitized():
    client, _ = make_client(FakeProvider(review_error="secret-token failure"))
    response = client.post("/api/reviews", json={"code": "x = 1\n"})
    assert response.status_code == 502
    assert "secret-token" not in response.text

    client, _ = make_client(FakeProvider(fix_error="private failure detail"))
    _, review_id = complete_workflow_for_fix_failure(client)
    failed = client.post(
        f"/api/reviews/{review_id}/findings/finding-1/fixes", json={}
    )
    assert failed.status_code == 503
    assert "private failure" not in failed.text


def complete_workflow_for_fix_failure(client):
    response = client.post("/api/reviews", json={"code": "x = 1\n"})
    return None, response.json()["review_id"]


def test_health_and_import_do_not_initialize_model(monkeypatch):
    from mcp_server import orchestrator

    def forbidden():
        raise AssertionError("health/import must not initialize Qwen")

    monkeypatch.setattr(orchestrator, "_get_llm", forbidden)
    module = importlib.import_module("mcp_server.http_api")
    default_app = module.create_app()
    response = TestClient(default_app).get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["provider_initialized"] is False


def test_mcp_tools_and_http_share_the_same_process_state():
    from mcp_server import server

    client, _ = make_client(state=PROCESS_STATE)
    assert server._REVIEW_CACHE is PROCESS_STATE.reviews
    assert server._FIX_CANDIDATE_CACHE is PROCESS_STATE.candidates
    response = client.post(
        "/api/reviews", json={"code": "def value():\n    return 1\n"}
    )
    review_id = response.json()["review_id"]
    assert review_id in server._REVIEW_CACHE
    candidate = client.post(
        f"/api/reviews/{review_id}/findings/finding-1/fixes", json={}
    )
    assert candidate.status_code == 201
    assert candidate.json()["candidate_id"] in server._FIX_CANDIDATE_CACHE
