import inspect
from unittest.mock import patch

from rag.adapters.base import CanonicalRecord
from rag.ingest import chunk_record
from rag.retrieve import retrieve_context


class FakeCollection:
    def __init__(self):
        self.query_args = None

    def count(self):
        return 5

    def query(self, **kwargs):
        self.query_args = kwargs
        return {"documents": [["context one", "context two", "context three", "context four"]]}


class FakeTokenizer:
    def encode(self, text, add_special_tokens=False):
        return text.split()

    def decode(self, token_ids, **kwargs):
        return " ".join(token_ids)


def test_public_retrieval_signature_is_code_only():
    assert list(inspect.signature(retrieve_context).parameters) == ["code"]


def test_empty_code_does_not_open_vector_store():
    with patch("rag.retrieve._get_collection", side_effect=AssertionError):
        assert retrieve_context("  ") == []


def test_retrieval_requests_four_contexts():
    collection = FakeCollection()
    with patch("rag.retrieve._get_collection", return_value=collection):
        result = retrieve_context("def calculate_total(items): return sum(items)")

    assert result == ["context one", "context two", "context three", "context four"]
    assert collection.query_args["n_results"] == 4


def test_chunking_respects_token_limit_and_overlap():
    record = CanonicalRecord(
        id="sample",
        text=" ".join(f"token{i}" for i in range(450)),
        source="test",
        language="python",
        type="review_comment",
    )

    chunks = chunk_record(record, FakeTokenizer(), max_tokens=200, overlap=20)

    assert len(chunks) == 3
    assert all(int(chunk.metadata["chunk_tokens"]) <= 200 for chunk in chunks)
    assert chunks[0].id == "sample_chunk_0"
    assert chunks[-1].id == "sample_chunk_2"
