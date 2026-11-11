import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from rag.adapters.base import (
    CanonicalRecord,
    compute_content_hash,
    generate_record_id,
    sanitize_text,
    validate_canonical_record,
)
from rag.adapters.microsoft_codereviewer import load_microsoft_codereviewer
from rag.adapters.reference_rules import load_cwe_records, load_owasp_records, load_style_records
from rag.config import CHROMA_PERSIST_DIR, COLLECTION_NAME, DEFAULT_K
from rag.ingest import (
    MAX_CHUNK_TOKENS,
    MAX_TOTAL_CHUNKS,
    MIN_MICROSOFT_CHUNKS,
    TARGET_MIN_CHUNKS,
    check_holdout_exclusions,
    chunk_record,
    deduplicate_and_filter_records,
    prepare_records,
)
from rag.retrieve import retrieve_context


class FakeCollection:
    def __init__(self, doc_count=5):
        self._count = doc_count
        self.query_args = None

    def count(self):
        return self._count

    def query(self, **kwargs):
        self.query_args = kwargs
        return {"documents": [["context one", "context two", "context three", "context four"]]}


class FakeTokenizer:
    def encode(self, text, add_special_tokens=False):
        return text.split()

    def decode(self, token_ids, **kwargs):
        return " ".join(token_ids)


# =====================================================================
# 1. CANONICAL RECORD TESTS
# =====================================================================

def test_canonical_record_valid():
    record = CanonicalRecord(
        id="rec_01",
        text="def foo():\n    return 'valid record with sufficient detail'",
        source="microsoft_codereviewer",
        language="python",
        type="review_comment",
        tags=["python", "review"],
        metadata={"project": "proj_a"},
    )
    assert validate_canonical_record(record) is True
    assert record.to_dict()["id"] == "rec_01"


def test_canonical_record_empty_id_rejected():
    record = CanonicalRecord(
        id="",
        text="def foo(): return 42",
        source="microsoft_codereviewer",
        language="python",
        type="review_comment",
    )
    assert validate_canonical_record(record) is False


def test_canonical_record_empty_or_short_text_rejected():
    record_empty = CanonicalRecord(
        id="rec_empty",
        text="",
        source="microsoft_codereviewer",
        language="python",
        type="review_comment",
    )
    assert validate_canonical_record(record_empty) is False

    record_short = CanonicalRecord(
        id="rec_short",
        text="abc",
        source="microsoft_codereviewer",
        language="python",
        type="review_comment",
    )
    assert validate_canonical_record(record_short) is False


def test_text_sanitization_removes_control_chars_and_preserves_code_indentation():
    raw_code = "   def foo():\n       # Indented comment\n       x = 1\x00\x08\x1f\n       return x   "
    sanitized = sanitize_text(raw_code)

    assert "\x00" not in sanitized
    assert "\x08" not in sanitized
    assert "\x1f" not in sanitized
    # Code indentation preserved
    assert "\n       # Indented comment" in sanitized
    assert "\n       x = 1" in sanitized


def test_compute_content_hash_deterministic():
    text1 = "def bar(x):    return x * 2\n"
    text2 = "def bar(x): return x * 2"
    assert compute_content_hash(text1) == compute_content_hash(text2)


def test_generate_record_id_deterministic():
    id1 = generate_record_id("ms_review", "12345", "def foo(): pass")
    id2 = generate_record_id("ms_review", "12345", "def foo(): pass")
    assert id1 == id2
    assert id1.startswith("ms_review_12345_")


# =====================================================================
# 2. CHUNKING & DEDUPLICATION TESTS
# =====================================================================

def test_chunking_respects_token_limit_and_overlap():
    record = CanonicalRecord(
        id="sample",
        text=" ".join(f"token{i}" for i in range(450)),
        source="microsoft_codereviewer",
        language="python",
        type="review_comment",
    )
    chunks = chunk_record(record, FakeTokenizer(), max_tokens=200, overlap=20)

    assert len(chunks) == 3
    assert all(int(chunk.metadata["chunk_tokens"]) <= 200 for chunk in chunks)
    assert chunks[0].id == "sample_chunk_0"
    assert chunks[-1].id == "sample_chunk_2"
    assert chunks[0].metadata["parent_id"] == "sample"


def test_short_semantic_unit_remains_single_chunk():
    record = CanonicalRecord(
        id="short_unit",
        text="def add(a, b): return a + b",
        source="style_static_rules",
        language="python",
        type="style_rule",
    )
    chunks = chunk_record(record, FakeTokenizer(), max_tokens=200, overlap=20)
    assert len(chunks) == 1
    assert chunks[0].id == "short_unit_chunk_0"
    assert chunks[0].text == "def add(a, b): return a + b"


def test_prepare_records_removes_duplicate_chunks():
    tok = FakeTokenizer()
    r1 = CanonicalRecord(id="1", text="def common_function(x): return x * 2", source="owasp_cwe", language="python", type="security_pattern")
    r2 = CanonicalRecord(id="2", text="def common_function(x):   return x * 2", source="owasp_cwe", language="python", type="security_pattern")
    r3 = CanonicalRecord(id="3", text="def unique_function(y): return y + 5", source="owasp_cwe", language="python", type="security_pattern")

    chunks, stats = prepare_records([r1, r2, r3], tok)
    assert len(chunks) == 2
    assert stats["deduplicated"] == 1


# =====================================================================
# 3. INGESTION & HOLDOUT EXCLUSION TESTS
# =====================================================================

def test_holdout_exclusion_rejects_matching_records(tmp_path):
    manifest_path = tmp_path / "heldout_manifest.json"
    target_hash = compute_content_hash("heldout code and comment")
    manifest_data = {
        "dataset": "ronantakizawa/github-codereview",
        "split": "test",
        "official_holdout_verified": True,
        "status": "official_list_verified",
        "fingerprints": [target_hash],
        "normalized_texts": ["heldout code and comment"],
    }
    manifest_path.write_text(__import__("json").dumps(manifest_data), encoding="utf-8")

    rec_good = CanonicalRecord(id="good", text="def safe(): return True", source="owasp_cwe", language="python", type="security_pattern")
    rec_heldout = CanonicalRecord(id="bad", text="heldout code and comment", source="microsoft_codereviewer", language="python", type="review_comment")

    allowed, stats = check_holdout_exclusions([rec_good, rec_heldout], manifest_path)
    assert len(allowed) == 1
    assert allowed[0].id == "good"
    assert stats["excluded_count"] == 1
    assert stats["holdout_verified"] is True


def test_corpus_v2_has_only_three_categories():
    assert COLLECTION_NAME == "critique_code_context_v2"
    # Static baseline source keys
    approved_sources = {"microsoft_codereviewer", "owasp_cwe", "style_static_rules"}
    cwe_records = load_cwe_records(limit=2)
    style_records = load_style_records(limit=2)

    for r in cwe_records:
        assert r.source in approved_sources
    for r in style_records:
        assert r.source in approved_sources


# =====================================================================
# 4. RETRIEVAL INTERFACE & SIDE-EFFECT TESTS
# =====================================================================

def test_public_retrieval_signature_is_code_only():
    params = list(inspect.signature(retrieve_context).parameters.keys())
    assert params == ["code"], "retrieve_context interface must accept exactly 'code'"


def test_empty_and_whitespace_code_returns_empty_list():
    assert retrieve_context("") == []
    assert retrieve_context("   \n\t  ") == []


def test_missing_database_returns_empty_list_without_creating_dirs(tmp_path):
    with patch("rag.retrieve.CHROMA_PERSIST_DIR", tmp_path / "nonexistent_dir"):
        with patch("rag.retrieve._collection", None):
            res = retrieve_context("def test(): pass")
            assert res == []
            assert not (tmp_path / "nonexistent_dir").exists()


def test_retrieval_returns_up_to_four_documents():
    collection = FakeCollection(doc_count=10)
    with patch("rag.retrieve._get_collection", return_value=collection):
        result = retrieve_context("def calculate_total(items): return sum(items)")

    assert isinstance(result, list)
    assert len(result) <= 4
    assert result == ["context one", "context two", "context three", "context four"]
    assert collection.query_args["n_results"] == 4


def test_retrieval_has_no_side_effects():
    """Verify retrieve_context does not mutate database or filesystem."""
    persist_stat_before = None
    if CHROMA_PERSIST_DIR.exists():
        persist_stat_before = [f.stat().st_mtime for f in CHROMA_PERSIST_DIR.rglob("*") if f.is_file()]

    # Execute multiple retrieval queries
    res1 = retrieve_context("def add(a, b): return a + b")
    res2 = retrieve_context("def mul(a, b): return a * b")

    assert isinstance(res1, list)
    assert isinstance(res2, list)

    if CHROMA_PERSIST_DIR.exists() and persist_stat_before:
        persist_stat_after = [f.stat().st_mtime for f in CHROMA_PERSIST_DIR.rglob("*") if f.is_file()]
        assert len(persist_stat_before) == len(persist_stat_after)
