"""
CRITIQUE - Standalone Pipeline Integration Test
=================================================

Feeds 4 sample code snippets (different bug types) through the full
RAG -> LLM -> Classification pipeline WITHOUT needing a real GitHub
webhook. Uses mock PR data so you can sanity-check everything works
before testing on a real pull request.

Test cases:
  1. SQL Injection          -> expect Hard (Security)
  2. Off-by-one logic error -> expect Hard (Bug / Logic)
  3. Resource leak          -> expect Hard (Resource Leak)
  4. Style / naming issue   -> expect Easy (Style)

Usage:
  cd C:\\Major-Project
  python test_pipeline.py
"""

import os
os.environ["USE_TF"] = "0"
os.environ["USE_TORCH"] = "1"

import sys
import logging
import time

# Fix Windows console encoding for Unicode
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Configure readable logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_pipeline")


# -------------------------------------------------------------
# TEST CASES  -- 4 code snippets with different bug types
# -------------------------------------------------------------

TEST_CASES = [
    {
        "name": "SQL Injection Vulnerability",
        "expected": "Hard",
        "file_path": "services/user_service.py",
        "code": '''def get_user_by_email(email):
    """Fetch user record by email address."""
    query = "SELECT * FROM users WHERE email = '" + email + "'"
    cursor = db.cursor()
    cursor.execute(query)
    return cursor.fetchone()
''',
    },
    {
        "name": "Off-by-One Logic Error",
        "expected": "Hard",
        "file_path": "utils/pagination.py",
        "code": '''def get_page_items(items, page_size, page_num):
    """Return items for a given page number (1-indexed)."""
    start = page_num * page_size        # BUG: should be (page_num - 1) * page_size
    end = start + page_size
    if start > len(items):
        return []
    return items[start:end]
''',
    },
    {
        "name": "Resource Leak  -- Unclosed File Handle",
        "expected": "Hard",
        "file_path": "io/config_reader.py",
        "code": '''def load_config(path):
    """Load JSON config from disk."""
    f = open(path, "r")
    data = json.loads(f.read())
    # f.close() is never called  -- if json.loads() raises,
    # the file descriptor leaks
    return data
''',
    },
    {
        "name": "Pure Style / Naming Issue",
        "expected": "Easy",
        "file_path": "models/user.py",
        "code": '''def calcAge(DOB):
    from datetime import date
    Today = date.today()
    age = Today.year - DOB.year
    return age
''',
    },
]


def print_separator(char="=", width=75):
    print(char * width)


def print_section(title, content, indent=4):
    """Print a labeled section with indented content."""
    print(f"\n  {title}:")
    for line in str(content).splitlines():
        print(" " * indent + line)


def main():
    from mcp_server.orchestrator import (
        run_review_pipeline,
        save_pipeline_results,
        classify_review,
    )

    print_separator()
    print("  CRITIQUE  -- STANDALONE PIPELINE INTEGRATION TEST")
    print("  Testing RAG  -> LLM  -> Easy/Hard Classification")
    print_separator()

    total_start = time.time()
    results = []
    passed = 0
    failed = 0

    for i, tc in enumerate(TEST_CASES, start=1):
        print(f"\n{'-' * 75}")
        print(f"  TEST CASE {i}/{len(TEST_CASES)}: {tc['name']}")
        print(f"  File: {tc['file_path']}")
        print(f"  Expected classification: {tc['expected']}")
        print(f"{'-' * 75}")

        # Show the input code
        print_section("INPUT CODE", tc["code"])

        # Run the full pipeline (no GitHub post, no desktop notification)
        t0 = time.time()
        result = run_review_pipeline(
            code=tc["code"],
            file_path=tc["file_path"],
            post_to_github=False,
            notify_desktop=False,
        )
        elapsed = time.time() - t0

        # Show RAG context hits
        print_section(
            f"RAG CONTEXT ({len(result.retrieved_context)} snippets)",
            "\n".join(
                f"[{j+1}] {ctx[:100]}..."
                for j, ctx in enumerate(result.retrieved_context)
            ) if result.retrieved_context else "(no context retrieved)"
        )

        # Show LLM output
        print_section("LLM REVIEW OUTPUT", result.llm_output)

        # Show classification
        match = result.classification == tc["expected"]
        status = "[OK] PASS" if match else "[!]  MISMATCH"
        if match:
            passed += 1
        else:
            failed += 1

        print(f"\n  CLASSIFICATION: {result.classification}")
        print(f"  REASON:         {result.classification_reason}")
        print(f"  EXPECTED:       {tc['expected']}")
        print(f"  STATUS:         {status}")
        print(f"  TIME:           {elapsed:.1f}s")

        results.append(result)

    # -- Summary ----------------------------------------------
    total_elapsed = time.time() - total_start

    print(f"\n{'=' * 75}")
    print(f"  TEST SUMMARY")
    print(f"{'=' * 75}")
    print(f"  Total test cases:  {len(TEST_CASES)}")
    print(f"  Passed:            {passed}")
    print(f"  Mismatched:        {failed}")
    print(f"  Total time:        {total_elapsed:.1f}s")
    print()

    for i, (tc, result) in enumerate(zip(TEST_CASES, results), start=1):
        match = result.classification == tc["expected"]
        icon = "[OK]" if match else "[!]"
        print(f"  {icon} Case {i}: {tc['name']:<45}  -> {result.classification} (expected {tc['expected']})")

    # -- Save results to log file -----------------------------
    log_path = save_pipeline_results(results, label="integration_test")
    print(f"\n  Pipeline log saved to: {log_path}")
    print(f"{'=' * 75}")

    # Exit with non-zero if any classification mismatched
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
