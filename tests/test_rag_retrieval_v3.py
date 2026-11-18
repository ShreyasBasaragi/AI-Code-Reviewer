from __future__ import annotations

import copy
import sys
from types import SimpleNamespace

import pytest

from rag import retrieve
from rag.retrieve_v3 import RAGConfigurationError, RetrievalConfig, V3Retriever


class FakeEmbedder:
    def encode(self, texts, **_kwargs):
        return [[0.1, 0.2] for _ in texts]


class FakeCollection:
    name = "critique_code_context_v3"

    def __init__(self, rows, siblings=None):
        self.rows = rows
        self.siblings = siblings or {}
        self.calls = []

    def count(self):
        return len(self.rows)

    def query(self, *, query_embeddings, n_results, include):
        self.calls.append(("query", n_results, tuple(include)))
        chosen = self.rows[:n_results]
        return {"ids": [[row[0] for row in chosen] for _ in query_embeddings],
                "documents": [[row[1] for row in chosen] for _ in query_embeddings],
                "metadatas": [[copy.deepcopy(row[2]) for row in chosen] for _ in query_embeddings],
                "distances": [[row[3] for row in chosen] for _ in query_embeddings]}

    def get(self, *, where, include):
        self.calls.append(("get", where, tuple(include)))
        return self.siblings.get(where["record_id"], {"ids": [], "documents": [], "metadatas": []})


def row(chunk, text, parent, family="microsoft_historical_reviews", language="python",
        role="record_content", source_field="content", index=0, distance=0.1):
    return (chunk, text, {"record_id": parent, "source_family": family,
                          "source_category": "historical_pr", "source_document_id": f"doc-{parent}",
                          "language": language, "role": role, "source_field": source_field,
                          "chunk_index": index, "chunk_count": 1}, distance)


def build(rows, *, strategy="v3_optimized", config_changes=None, siblings=None):
    kwargs = {"strategy": strategy}
    kwargs.update(config_changes or {})
    collection = FakeCollection(rows, siblings)
    retriever = V3Retriever(collection=collection, embedder=FakeEmbedder(),
                            config=RetrievalConfig(**kwargs))
    return retriever, collection


def test_v2_is_default_and_v3_is_explicit(monkeypatch):
    monkeypatch.delenv("AI_CODE_REVIEWER_RAG_VERSION", raising=False)
    assert retrieve.retrieve_evidence.__name__ == "retrieve_evidence"
    from rag.retrieve_v3 import retrieval_backend_selection
    assert retrieval_backend_selection() == ("v2", "v2_baseline")
    assert retrieval_backend_selection("v3", "v3_baseline") == ("v3", "v3_baseline")


def test_invalid_version_and_strategy_fail_clearly():
    from rag.retrieve_v3 import retrieval_backend_selection
    with pytest.raises(RAGConfigurationError, match="unsupported RAG version"):
        retrieval_backend_selection("v4")
    with pytest.raises(RAGConfigurationError, match="only the v2_baseline"):
        retrieval_backend_selection("v2", "v3_optimized")


def test_explicit_router_selection_and_no_v3_fallback(monkeypatch, tmp_path):
    from rag import retrieve_v3
    monkeypatch.setattr(retrieve, "_retrieve_evidence_v2", lambda code: [code])
    monkeypatch.setattr(retrieve_v3, "retrieve_evidence_v3", lambda code, **kwargs: [kwargs["strategy"]])
    assert retrieve.retrieve_evidence("sample") == ["sample"]
    assert retrieve.retrieve_evidence("sample", version="v3", strategy="v3_baseline") == ["v3_baseline"]
    missing_path = tmp_path / "no-v3-db"
    retriever = V3Retriever(db_path=missing_path, collection=None, embedder=FakeEmbedder())
    with pytest.raises(RAGConfigurationError, match="v3 Chroma database is missing"):
        retriever.retrieve("sample")
    assert not missing_path.exists()


def test_missing_v3_collection_fails_without_any_create_call(monkeypatch, tmp_path):
    calls = []

    class Client:
        def get_collection(self, **_kwargs):
            calls.append("get_collection")
            raise ValueError("missing")

        def create_collection(self, **_kwargs):
            calls.append("create_collection")

        def get_or_create_collection(self, **_kwargs):
            calls.append("get_or_create_collection")

    fake_chromadb = SimpleNamespace(PersistentClient=lambda **_kwargs: Client())
    monkeypatch.setitem(sys.modules, "chromadb", fake_chromadb)
    path = tmp_path / "existing-db-copy"
    path.mkdir()
    retriever = V3Retriever(db_path=path)
    with pytest.raises(RAGConfigurationError, match="cannot open existing v3 collection"):
        retriever._open()
    assert calls == ["get_collection"]


def test_v3_baseline_preserves_top_k_and_does_not_expand_parents():
    rows = [row(f"c{i}", f"text {i}", f"p{i}", distance=i / 10) for i in range(6)]
    retriever, collection = build(rows, strategy="v3_baseline")
    evidence, diagnostics = retriever.retrieve_with_diagnostics("x = 1")
    assert [item.evidence_id for item in evidence] == ["c0", "c1", "c2", "c3"]
    assert not any(call[0] == "get" for call in collection.calls)
    assert diagnostics["strategy"] == "v3_baseline"
    assert diagnostics["retrieval_config"]["parent_aware"] is False


def test_parent_dedup_and_linked_comment_context_keep_real_evidence_id():
    rows = [row("patch-0", "Patch: call()", "review-1", role="patch_hunk", source_field="patch", distance=.1),
            row("patch-1", "next hunk", "review-1", role="patch_hunk", source_field="patch", index=1, distance=.11),
            row("other", "different guidance", "review-2", family="pep8", distance=.12)]
    siblings = {"review-1": {"ids": ["comment-0", "patch-1"],
                             "documents": ["Reviewer comment: handle the error", "next hunk"],
                             "metadatas": [{"role": "review_comment", "source_field": "reviewer_comment", "chunk_index": 0},
                                           rows[1][2]]}}
    retriever, _ = build(rows, siblings=siblings)
    evidence, diagnostics = retriever.retrieve_with_diagnostics("def call(): pass")
    assert evidence[0].evidence_id == "patch-0"
    assert "handle the error" in evidence[0].text
    assert evidence[0].metadata["assembled_chunk_ids"][:2] == ["patch-0", "comment-0"]
    assert evidence[0].metadata["assembled_chunk_ids"] == ["patch-0", "comment-0", "patch-1"]
    assert len({item.metadata["parent_id"] for item in evidence}) == len(evidence)
    assert diagnostics["parent_deduplication_count"] == 1


def test_language_ranking_is_conservative_and_diagnostic():
    rows = [row("java", "java example", "p-java", language="java", distance=.1),
            row("py", "python example", "p-py", language="python", distance=.13),
            row("unknown", "unknown sample", "p-unknown", language="unknown", distance=.14)]
    retriever, _ = build(rows, config_changes={"top_k": 2})
    evidence, diagnostics = retriever.retrieve_with_diagnostics("def f(): return 1", language="python")
    assert evidence[0].evidence_id == "py"
    assert diagnostics["input_language"]["origin"] == "upstream_explicit"
    assert diagnostics["language_distribution"]["confident_mismatch"] == 1
    assert diagnostics["language_distribution"]["exact_match"] == 1


def test_context_budget_excludes_items_that_do_not_fit_and_reports_estimator():
    rows = [row("large", "x" * 500, "p1", distance=.1)]
    retriever, _ = build(rows, config_changes={"evidence_token_budget": 20, "prompt_token_reserve": 1})
    evidence, diagnostics = retriever.retrieve_with_diagnostics("a")
    assert evidence == []
    assert diagnostics["candidates"][0]["selection_reason"] == "excluded_single_item_over_budget"
    assert diagnostics["retrieval_config"]["token_estimator"].startswith("UTF-8 bytes")


def test_source_diversity_is_soft_and_no_fixed_source_quota():
    rows = [row("m1", "microsoft one", "m1", distance=.1),
            row("m2", "microsoft two", "m2", distance=.11),
            row("m3", "microsoft three", "m3", distance=.2),
            row("pep", "style guidance", "pep", family="pep8", distance=.21)]
    retriever, _ = build(rows, config_changes={"top_k": 3})
    evidence, _ = retriever.retrieve_with_diagnostics("def f(): return 2")
    assert [item.evidence_id for item in evidence] == ["m1", "m2", "pep"]


def test_bandit_applicability_is_diagnostic_not_a_defect_label():
    metadata = {"record_id": "bandit-1", "source_family": "bandit", "source_category": "security",
                "language": "python", "role": "record_content", "source_field": "content", "rule_id": "B602"}
    rows = [("b602", "subprocess call with shell=True may be unsafe", metadata, .1)]
    retriever, _ = build(rows)
    evidence, diagnostics = retriever.retrieve_with_diagnostics("subprocess.run(command, shell=False)")
    assert diagnostics["candidates"][0]["applicability"]["status"] == "likely_mismatch"
    assert evidence[0].evidence_id == "b602"


def test_empty_and_stable_order_and_read_only_calls():
    rows = [row("b", "B", "pb", family="pep8", distance=.2), row("a", "A", "pa", distance=.1)]
    retriever, collection = build(rows)
    assert retriever.retrieve("   ") == []
    first = retriever.retrieve("x = 2")
    second = retriever.retrieve("x = 2")
    assert [item.evidence_id for item in first] == [item.evidence_id for item in second]
    assert all(call[0] in {"query", "get"} for call in collection.calls)


def test_long_patch_windows_are_bounded_and_unicode_estimate_is_explicit():
    rows = [row("one", "reference text", "one")]
    retriever, _ = build(rows)
    _evidence, diagnostics = retriever.retrieve_with_diagnostics("line = 'é'\n" * 1800)
    assert diagnostics["query_window_count"] == 5
    assert retriever._token_estimate("ééé", 3) == 2


def test_microsoft_source_family_is_not_reported_as_other():
    from evaluation.runner import _source_category
    evidence = retrieve.RetrievedEvidence(
        evidence_id="v3_chunk", source_family="microsoft_historical_reviews",
        source_url=None, rule_identifier=None, text="review and patch",
        metadata={"source_family": "microsoft_historical_reviews"},
    )
    assert _source_category(evidence) == "historical_pr"


def test_evaluation_v3_copy_selection_is_explicit_and_uses_injected_strategy(monkeypatch, tmp_path):
    from evaluation import runner
    captured = {}

    class FakeRetriever:
        def __init__(self, *, db_path, config):
            captured["db_path"] = db_path
            captured["config"] = config

        def retrieve(self, code):
            return [code]

    monkeypatch.setattr("rag.retrieve_v3.V3Retriever", FakeRetriever)
    copy_dir = tmp_path / "v3-copy"
    copy_dir.mkdir()
    retrieve_fn = runner._copy_retriever(copy_dir, version="v3", strategy="v3_baseline",
                                         retrieval_config=RetrievalConfig(strategy="v3_baseline"))
    assert retrieve_fn("input") == ["input"]
    assert captured["db_path"] == copy_dir.resolve()
    assert captured["config"].strategy == "v3_baseline"
