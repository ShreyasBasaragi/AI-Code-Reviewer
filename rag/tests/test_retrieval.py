import pytest
from rag.adapters.base import CanonicalRecord, validate_canonical_record, sanitize_text
from rag.adapters.microsoft_codereviewer import load_microsoft_codereviewer
from rag.adapters.reference_rules import load_cwe_records, load_style_records
from rag.ingest import deduplicate_and_filter_records
from rag.retrieve import retrieve_context


def test_canonical_record_validation():
    valid_rec = CanonicalRecord(
        id="test_1",
        text="Valid code review text containing sufficient detail.",
        source="microsoft_codereviewer",
        language="python",
        type="review_comment"
    )
    assert validate_canonical_record(valid_rec) is True

    short_rec = CanonicalRecord(
        id="test_2",
        text="short",
        source="microsoft_codereviewer",
        language="python",
        type="review_comment"
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


def test_reference_rules_adapters():
    cwe = load_cwe_records(limit=5)
    assert isinstance(cwe, list)
    if len(cwe) > 0:
        assert cwe[0].source == "owasp_cwe"

    style = load_style_records(limit=5)
    assert isinstance(style, list)
    if len(style) > 0:
        assert style[0].source == "style_static_rules"


def test_deduplication_and_filtering():
    r1 = CanonicalRecord(id="1", text="Duplicate review comment snippet text.", source="microsoft_codereviewer", language="python", type="review_comment")
    r2 = CanonicalRecord(id="2", text="Duplicate review comment snippet text.", source="microsoft_codereviewer", language="python", type="review_comment")
    r3 = CanonicalRecord(id="3", text="Unique review comment snippet text.", source="microsoft_codereviewer", language="python", type="review_comment")

    accepted, stats = deduplicate_and_filter_records([r1, r2, r3])
    assert len(accepted) == 2
    assert stats["records_deduplicated"] == 1


def test_frozen_retrieve_context_interface():
    code_sample = "def calculate_sum(a, b=[]): return a + b"
    results = retrieve_context(code_sample)

    assert isinstance(results, list), "Return value must be a list"
    assert len(results) <= 4, "Returned items must be <= 4"
    for item in results:
        assert isinstance(item, str), "Each item in returned list must be a string"


def test_retrieve_context_empty_input():
    assert retrieve_context("") == []
    assert retrieve_context("   \n\t  ") == []
