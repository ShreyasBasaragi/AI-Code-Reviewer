import pytest
from rag.augment import augment_context, clean_retrieved_snippets
from rag.retrieve import retrieve_context


def test_augment_context_basic():
    code_input = "def add(a, b=[]): return a + b"
    snippets = [
        "Code Pattern: Mutable Default Arguments Anti-Pattern\nNever use [] as default.",
        "Python Style Rule: 4. Default Parameter Values"
    ]

    result = augment_context(code_input, snippets)

    assert "# CURRENT CODE / PR DIFF" in result
    assert "def add(a, b=[]): return a + b" in result
    assert "# RELEVANT HISTORICAL CODE REVIEW CONTEXT" in result
    assert "[Example 1]" in result
    assert "Mutable Default Arguments Anti-Pattern" in result
    assert "[Example 2]" in result
    assert "INSTRUCTION FOR DOWNSTREAM LLM:" in result


def test_augment_context_empty_retrieval():
    code_input = "x = 42"
    result = augment_context(code_input, [])

    assert "# CURRENT CODE / PR DIFF" in result
    assert "x = 42" in result
    assert "No additional relevant historical review context retrieved." in result


def test_augment_context_deduplication():
    snippets = [
        "Duplicate review comment text snippet here.",
        "Duplicate review comment text snippet here.",
        "Unique review comment text snippet here."
    ]
    cleaned = clean_retrieved_snippets(snippets)
    assert len(cleaned) == 2


def test_augment_context_determinism():
    code_input = "try: pass except: pass"
    snippets = ["Bare exception anti-pattern review comment"]

    out1 = augment_context(code_input, snippets)
    out2 = augment_context(code_input, snippets)

    assert out1 == out2, "Augmentation must be 100% deterministic"


def test_end_to_end_rag_flow():
    target_code = "def process_data(val, cache={}): cache[val] = True"

    # Step 1: Retrieve
    retrieved = retrieve_context(target_code, k=3)
    assert isinstance(retrieved, list)

    # Step 2: Augment
    augmented = augment_context(target_code, retrieved)

    assert isinstance(augmented, str)
    assert "def process_data" in augmented
    assert "# RELEVANT HISTORICAL CODE REVIEW CONTEXT" in augmented
    assert "INSTRUCTION FOR DOWNSTREAM LLM:" in augmented
