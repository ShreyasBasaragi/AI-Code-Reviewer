import json
from typing import List

import pytest

from mcp_server import orchestrator


class FakeLLM:
    def __init__(self, response='{"summary":"No issues identified.","findings":[]}'):
        self.response = response
        self.calls = []

    def analyze_code(self, code: str, context: List[str]) -> str:
        self.calls.append((code, context))
        return self.response


def test_shared_pipeline_success_uses_injected_dependencies():
    llm = FakeLLM()
    retrieval_calls = []

    def retriever(code):
        retrieval_calls.append(code)
        return ["evidence snippet"]

    result = orchestrator.run_review_pipeline(
        code="print('hello')",
        llm=llm,
        retriever=retriever,
        notify_desktop=False,
    )

    assert result.llm_output == llm.response
    assert result.review_response.valid is True
    assert result.retrieved_context == ["evidence snippet"]
    assert retrieval_calls == ["print('hello')"]
    assert llm.calls == [("print('hello')", ["evidence snippet"])]
    assert result.comment_posted is False


def test_shared_pipeline_passes_empty_retrieval_to_model():
    llm = FakeLLM()
    result = orchestrator.run_review_pipeline(
        code="x = 1",
        llm=llm,
        retriever=lambda _code: [],
        notify_desktop=False,
    )

    assert result.retrieved_context == []
    assert llm.calls == [("x = 1", [])]
    assert result.llm_output == llm.response


def test_shared_pipeline_continues_after_retrieval_failure():
    llm = FakeLLM()
    secret_marker = "private-retriever-secret-marker"

    def failing_retriever(_code):
        raise RuntimeError(secret_marker)

    result = orchestrator.run_review_pipeline(
        code="x = 1",
        llm=llm,
        retriever=failing_retriever,
        notify_desktop=False,
    )

    assert result.retrieved_context == []
    assert llm.calls == [("x = 1", [])]
    assert result.llm_output == llm.response
    assert result.review_response.valid is True
    assert any("retrieval_failure" in error for error in result.review_response.processing_errors)
    assert all(secret_marker not in error for error in result.review_response.processing_errors)


def test_shared_pipeline_returns_safe_result_after_model_failure():
    class FailingLLM:
        def analyze_code(self, code: str, context: List[str]) -> str:
            raise RuntimeError("private-provider-secret-marker")

    result = orchestrator.run_review_pipeline(
        code="x = 1",
        llm=FailingLLM(),
        retriever=lambda _code: ["context"],
        notify_desktop=False,
    )

    assert result.llm_output.startswith("[WARNING]")
    assert "unable to complete automated analysis" in result.llm_output
    assert "private-provider-secret-marker" not in result.llm_output
    assert result.review_response.valid is False
    assert any("model_failure" in error for error in result.review_response.processing_errors)
    assert all("private-provider-secret-marker" not in error for error in result.review_response.processing_errors)
    assert result.classification == "Hard"
    assert result.comment_posted is False


@pytest.mark.parametrize("tool_name", ["review_code", "review_diff", "review_file"])
def test_mcp_tools_delegate_to_shared_pipeline_with_injected_fakes(
    tool_name, monkeypatch, tmp_path
):
    from mcp_server import server

    llm = FakeLLM()
    original_pipeline = orchestrator.run_review_pipeline
    pipeline_calls = []

    def injected_pipeline(**kwargs):
        pipeline_calls.append(kwargs)
        return original_pipeline(
            **kwargs,
            llm=llm,
            retriever=lambda _code: ["fake retrieved context"],
            notify_desktop=False,
        )

    monkeypatch.setattr(server, "run_review_pipeline", injected_pipeline)

    if tool_name == "review_code":
        response = server.review_code("x = 1", file_path="sample.py")
    elif tool_name == "review_diff":
        response = server.review_diff("diff --git a/a.py b/a.py")
    else:
        code_file = tmp_path / "sample.py"
        code_file.write_text("x = 1", encoding="utf-8")
        response = server.review_file(str(code_file))

    parsed = json.loads(response)
    assert parsed["summary"] == "No issues identified."
    assert parsed["raw_response"] == llm.response
    assert len(pipeline_calls) == 1
    assert llm.calls[0][1] == ["fake retrieved context"]
