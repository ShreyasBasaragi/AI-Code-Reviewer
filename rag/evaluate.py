"""Small smoke evaluation for the read-only retrieval interface."""
from typing import Any

from rag.retrieve import retrieve_context

EVALUATION_SNIPPETS = [
    {
        "category": "Mutable Default Parameter",
        "code": "def push_event(event, event_history=[]):\n    event_history.append(event)\n    return event_history",
        "expected_keywords": ["default", "mutable", "list", "append"],
    },
    {
        "category": "Unsafe SQL Query Formatting",
        "code": "def search_database(user_input):\n    sql = f'SELECT * FROM accounts WHERE name = \"{user_input}\"'\n    return db.execute(sql)",
        "expected_keywords": ["sql", "injection", "query", "parameter"],
    },
    {
        "category": "Bare Exception Swallow",
        "code": "try:\n    perform_transaction()\nexcept:\n    pass",
        "expected_keywords": ["except", "bare", "error", "catch"],
    },
    {
        "category": "Resource Leak",
        "code": "connection = SqlConnection(conn_string)\nconnection.Open()\n# connection is never closed",
        "expected_keywords": ["connection", "close", "using", "resource"],
    },
]


def evaluate_retrieval_quality() -> dict[str, Any]:
    """Report keyword coverage; a non-empty retrieval is not automatically a hit."""
    report = []
    for test in EVALUATION_SNIPPETS:
        contexts = retrieve_context(test["code"])
        combined = " ".join(contexts).lower()
        matches = [word for word in test["expected_keywords"] if word in combined]
        coverage = len(matches) / len(test["expected_keywords"])
        report.append(
            {
                "category": test["category"],
                "retrieved_count": len(contexts),
                "matched_keywords": matches,
                "keyword_coverage": coverage,
                "passed": coverage >= 0.5,
            }
        )

    passed = sum(item["passed"] for item in report)
    print(f"Retrieval smoke evaluation: {passed}/{len(report)} passed")
    for item in report:
        print(
            f"- {item['category']}: {item['retrieved_count']} results, "
            f"coverage={item['keyword_coverage']:.0%}, "
            f"passed={item['passed']}"
        )
    return {"total": len(report), "passed": passed, "results": report}


if __name__ == "__main__":
    evaluate_retrieval_quality()
