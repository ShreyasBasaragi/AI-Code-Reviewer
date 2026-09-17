import pytest
from rag.adapters.base import CanonicalRecord, validate_canonical_record, sanitize_text
from rag.adapters.github_codereview import load_github_codereview
from rag.adapters.microsoft_codereviewer import load_microsoft_codereviewer
from rag.ingest import deduplicate_and_filter_records
from rag.retrieve import retrieve_context, retrieve_context_for_repository
from rag.ingest_github import get_chromadb_source_breakdown


def test_canonical_record_validation():
    valid_rec = CanonicalRecord(
        id="test_1",
        text="Valid code review text containing sufficient detail.",
        source="unit_test",
        language="python",
        type="review"
    )
    assert validate_canonical_record(valid_rec) is True

    short_rec = CanonicalRecord(
        id="test_2",
        text="short",
        source="unit_test",
        language="python",
        type="review"
    )
    assert validate_canonical_record(short_rec) is False


def test_text_sanitization():
    raw_text = "   Function code \x00 with control chars. \n  "
    sanitized = sanitize_text(raw_text)
    assert "\x00" not in sanitized
    assert sanitized.startswith("Function code")


def test_microsoft_codereviewer_adapter():
    records = load_microsoft_codereviewer(max_per_category=10)
    assert isinstance(records, list)
    if len(records) > 0:
        rec = records[0]
        assert rec.source == "microsoft_codereviewer"
        assert len(rec.text) > 10


def test_deduplication_and_filtering():
    r1 = CanonicalRecord(id="1", text="Duplicate review comment snippet text.", source="src1", language="py", type="review")
    r2 = CanonicalRecord(id="2", text="Duplicate review comment snippet text.", source="src2", language="py", type="review")
    r3 = CanonicalRecord(id="3", text="Unique review comment snippet text.", source="src1", language="py", type="review")

    accepted, stats = deduplicate_and_filter_records([r1, r2, r3])
    assert len(accepted) == 2
    assert stats["records_deduplicated"] == 1


def test_frozen_retrieve_context_interface():
    code_sample = "def calculate_sum(a, b=[]): return a + b"
    results = retrieve_context(code_sample, k=3)

    assert isinstance(results, list), "Return value must be a list"
    assert len(results) <= 3, "Returned items must be <= k"
    for item in results:
        assert isinstance(item, str), "Each item in returned list must be a string"


def test_retrieve_context_for_repository_helper():
    code_sample = "def login(user): pass"
    results_all = retrieve_context_for_repository(code_sample, repository=None, k=3)
    assert isinstance(results_all, list)

    results_repo = retrieve_context_for_repository(code_sample, repository="psf/requests", k=3)
    assert isinstance(results_repo, list)


def test_chromadb_source_counts_preservation():
    breakdown = get_chromadb_source_breakdown()
    assert breakdown.get("github_codereview", 0) >= 1000, "github_codereview records must remain intact"
    assert breakdown.get("microsoft_codereviewer", 0) >= 1299, "microsoft_codereviewer records must remain intact"
    assert breakdown.get("style_guide", 0) >= 6, "style_guide records must remain intact"
