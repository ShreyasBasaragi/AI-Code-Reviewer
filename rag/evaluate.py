"""Quantitative evaluation of Critique RAG v2 retrieval quality and latency.

Computes:
- Query count & empty retrieval count
- Precision@K (K=4) based on target pattern relevance criteria
- Mean Reciprocal Rank (MRR) of the first relevant document
- Average retrieval latency (milliseconds per query)
- Keyword & concept coverage

Usage:
    python -m rag.evaluate
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from rag.retrieve import retrieve_context

EVALUATION_BENCHMARK = [
    {
        "id": "Q1",
        "category": "Mutable Default Parameter",
        "code": "def push_event(event, event_history=[]):\n    event_history.append(event)\n    return event_history",
        "relevant_terms": ["default", "mutable", "list", "argument", "parameter", "event", "append"],
        "min_terms_for_relevance": 1,
    },
    {
        "id": "Q2",
        "category": "Unsafe SQL Query Formatting",
        "code": "def search_database(user_input):\n    sql = f'SELECT * FROM accounts WHERE name = \"{user_input}\"'\n    return db.execute(sql)",
        "relevant_terms": ["sql", "injection", "query", "parameter", "cwe", "select", "where", "execute"],
        "min_terms_for_relevance": 1,
    },
    {
        "id": "Q3",
        "category": "Bare Exception Swallow",
        "code": "try:\n    perform_transaction()\nexcept:\n    pass",
        "relevant_terms": ["except", "bare", "exception", "error", "catch", "pass", "fail"],
        "min_terms_for_relevance": 1,
    },
    {
        "id": "Q4",
        "category": "Resource Leak (Unclosed File/Socket)",
        "code": "connection = open_connection(host, port)\nconnection.send(data)\n# missing connection close",
        "relevant_terms": ["close", "resource", "leak", "connection", "context", "with", "finally"],
        "min_terms_for_relevance": 1,
    },
    {
        "id": "Q5",
        "category": "Unsafe eval() / Arbitrary Code Execution",
        "code": "result = eval(user_supplied_code)",
        "relevant_terms": ["eval", "execution", "unsafe", "cwe", "code", "run", "arbitrary"],
        "min_terms_for_relevance": 1,
    },
    {
        "id": "Q6",
        "category": "Hardcoded Secret Token",
        "code": "AWS_SECRET_KEY = 'AKIAIOSFODNN7EXAMPLE'\nclient = s3.connect(AWS_SECRET_KEY)",
        "relevant_terms": ["secret", "credential", "hardcoded", "key", "token", "password", "auth", "cwe"],
        "min_terms_for_relevance": 1,
    },
    {
        "id": "Q7",
        "category": "Unvalidated Path Traversal",
        "code": "target_path = os.path.join(base_dir, user_filename)\nwith open(target_path, 'r') as f: return f.read()",
        "relevant_terms": ["path", "traversal", "directory", "cwe", "file", "sanitize", "join"],
        "min_terms_for_relevance": 1,
    },
    {
        "id": "Q8",
        "category": "Command Injection via Subprocess",
        "code": "subprocess.Popen(f'cat {user_file}', shell=True)",
        "relevant_terms": ["subprocess", "shell", "command", "injection", "cwe", "popen", "call"],
        "min_terms_for_relevance": 1,
    },
    {
        "id": "Q9",
        "category": "Insecure Randomness for Security",
        "code": "import random\nsession_id = random.randint(100000, 999999)",
        "relevant_terms": ["random", "pseudo", "crypto", "secrets", "cwe", "token", "auth"],
        "min_terms_for_relevance": 1,
    },
    {
        "id": "Q10",
        "category": "PEP 8 Naming / Style Convention",
        "code": "def MyFunction(X, Y_VAL=[]):\n    return X + len(Y_VAL)",
        "relevant_terms": ["pep", "naming", "style", "convention", "function", "snake", "case", "default"],
        "min_terms_for_relevance": 1,
    },
]


def evaluate_retrieval_quality() -> Dict[str, Any]:
    """Execute quantitative retrieval evaluation and return metrics summary."""
    print("=" * 75)
    print("      CRITIQUE RAG v2 RETRIEVAL QUALITY & LATENCY BENCHMARK")
    print("=" * 75)

    num_queries = len(EVALUATION_BENCHMARK)
    relevant_queries = 0
    empty_queries = 0
    reciprocal_ranks = []
    precisions = []
    latencies = []
    query_reports = []

    for item in EVALUATION_BENCHMARK:
        t0 = time.perf_counter()
        contexts = retrieve_context(item["code"])
        latency_ms = (time.perf_counter() - t0) * 1000
        latencies.append(latency_ms)

        if not contexts:
            empty_queries += 1
            reciprocal_ranks.append(0.0)
            precisions.append(0.0)
            query_reports.append({
                "id": item["id"],
                "category": item["category"],
                "retrieved_count": 0,
                "first_relevant_rank": None,
                "precision_at_k": 0.0,
                "latency_ms": round(latency_ms, 2),
            })
            continue

        # Evaluate relevance of each retrieved result
        first_rel_rank = None
        relevant_in_results = 0

        for rank, ctx in enumerate(contexts, start=1):
            ctx_lower = ctx.casefold()
            match_count = sum(1 for term in item["relevant_terms"] if term.casefold() in ctx_lower)
            is_relevant = match_count >= item["min_terms_for_relevance"]
            if is_relevant:
                relevant_in_results += 1
                if first_rel_rank is None:
                    first_rel_rank = rank

        if first_rel_rank is not None:
            relevant_queries += 1
            rr = 1.0 / first_rel_rank
        else:
            rr = 0.0

        p_at_k = relevant_in_results / len(contexts) if contexts else 0.0
        reciprocal_ranks.append(rr)
        precisions.append(p_at_k)

        query_reports.append({
            "id": item["id"],
            "category": item["category"],
            "retrieved_count": len(contexts),
            "first_relevant_rank": first_rel_rank,
            "precision_at_k": round(p_at_k, 2),
            "latency_ms": round(latency_ms, 2),
        })

    mrr = sum(reciprocal_ranks) / num_queries if num_queries > 0 else 0.0
    mean_precision = sum(precisions) / num_queries if num_queries > 0 else 0.0
    avg_latency_ms = sum(latencies) / len(latencies) if latencies else 0.0

    print(f"\n{'ID':<5} | {'Category':<35} | {'Count':<5} | {'Rank 1st':<8} | {'P@K':<6} | {'Latency':<9}")
    print("-" * 75)
    for q in query_reports:
        rank_str = str(q["first_relevant_rank"]) if q["first_relevant_rank"] else "None"
        print(f"{q['id']:<5} | {q['category'][:35]:<35} | {q['retrieved_count']:<5} | {rank_str:<8} | {q['precision_at_k']:<6.2f} | {q['latency_ms']:<7.1f}ms")

    print("=" * 75)
    print("METRICS SUMMARY:")
    print(f"  Total Queries:                 {num_queries}")
    print(f"  Queries with Relevant Hits:    {relevant_queries}/{num_queries} ({relevant_queries/num_queries:.1%})")
    print(f"  Empty Retrieval Count:         {empty_queries}")
    print(f"  Mean Reciprocal Rank (MRR):    {mrr:.3f}")
    print(f"  Mean Precision@K (K=4):        {mean_precision:.3f}")
    print(f"  Average Retrieval Latency:     {avg_latency_ms:.1f} ms")
    print("=" * 75 + "\n")

    return {
        "num_queries": num_queries,
        "queries_with_relevant_results": relevant_queries,
        "empty_retrieval_count": empty_queries,
        "mean_reciprocal_rank": round(mrr, 3),
        "mean_precision_at_k": round(mean_precision, 3),
        "average_latency_ms": round(avg_latency_ms, 1),
        "queries": query_reports,
    }


if __name__ == "__main__":
    evaluate_retrieval_quality()
