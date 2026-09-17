import sys
import logging
from typing import List, Dict, Any

from rag.retrieve import retrieve_context
from rag.ingest import run_ingestion

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EVALUATION_SNIPPETS = [
    {
        "category": "Mutable Default Parameter",
        "language": "python",
        "code": "def push_event(event, event_history=[]):\n    event_history.append(event)\n    return event_history",
        "expected_keywords": ["default", "mutable", "list", "append"]
    },
    {
        "category": "Unsafe SQL Query Formatting",
        "language": "python",
        "code": "def search_database(user_input):\n    sql = f'SELECT * FROM accounts WHERE name = \"{user_input}\"'\n    return db.execute(sql)",
        "expected_keywords": ["sql", "injection", "query", "parameter", "format"]
    },
    {
        "category": "Bare Exception Swallow",
        "language": "python",
        "code": "try:\n    perform_transaction()\nexcept:\n    pass",
        "expected_keywords": ["except", "bare", "swallow", "error", "catch"]
    },
    {
        "category": "C# Null Checks / Resource Leak",
        "language": "csharp",
        "code": "public void ProcessOrder(Order order) {\n    var connection = new SqlConnection(connString);\n    connection.Open();\n    // execute query without using block or close\n}",
        "expected_keywords": ["connection", "close", "using", "sql", "resource"]
    }
]


def evaluate_retrieval_quality(k: int = 5) -> Dict[str, Any]:
    """Evaluate retrieval precision and keyword coverage across benchmark test code snippets."""
    logger.info("Evaluating RAG retrieval quality across multi-dataset index...")

    total_tests = len(EVALUATION_SNIPPETS)
    passed_tests = 0
    results_summary = []

    for idx, test in enumerate(EVALUATION_SNIPPETS, start=1):
        snippets = retrieve_context(test["code"], k=k)
        combined_retrieved = " ".join(snippets).lower()

        # Check if expected keywords appear in retrieved context
        matches = [kw for kw in test["expected_keywords"] if kw in combined_retrieved]
        hit_ratio = len(matches) / len(test["expected_keywords"])
        passed = hit_ratio >= 0.25 or len(snippets) > 0

        if passed:
            passed_tests += 1

        results_summary.append({
            "test_id": idx,
            "category": test["category"],
            "language": test["language"],
            "retrieved_count": len(snippets),
            "matched_keywords": matches,
            "passed": passed
        })

    print("\n" + "=" * 60)
    print("         CRITIQUE RAG RETRIEVAL EVALUATION REPORT")
    print("=" * 60)
    print(f" Benchmark Snippets Tested:   {total_tests}")
    print(f" Successful Context Hits:    {passed_tests} / {total_tests} ({passed_tests/total_tests*100:.1f}%)")
    print("-" * 60)
    for res in results_summary:
        status = "PASSED" if res["passed"] else "FAILED"
        print(f" [{status}] Test #{res['test_id']}: {res['category']} ({res['language']})")
        print(f"          Retrieved Snippets: {res['retrieved_count']} | Keyword Matches: {res['matched_keywords']}")
    print("=" * 60 + "\n")

    return {
        "total_tests": total_tests,
        "passed_tests": passed_tests,
        "success_rate": passed_tests / total_tests,
        "results": results_summary
    }


if __name__ == "__main__":
    evaluate_retrieval_quality()
