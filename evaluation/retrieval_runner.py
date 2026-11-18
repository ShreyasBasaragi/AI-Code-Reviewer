"""Read-only offline comparison of v2/v3 retrieval strategies."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from rag.retrieve import retrieve_evidence_with_diagnostics
from rag.retrieve_v3 import RetrievalConfig, V3Retriever
from rag.config import PROJECT_ROOT

QUERY_PATH = Path(__file__).with_name("retrieval_queries_v1.jsonl")
DEFAULT_OUTPUT = Path(__file__).with_name("results")


def _read_queries(path: Path = QUERY_PATH) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _run_one(mode: str, queries: list[dict[str, Any]], *, v3_db: Path,
             include_validation: bool = True) -> dict[str, Any]:
    chosen = [q for q in queries if include_validation or q["split"] == "development"]
    v3 = None
    ablations = {
        "v3_optimized_no_parent_aware": {"parent_aware": False},
        "v3_optimized_no_language_aware": {"language_aware": False},
        "v3_optimized_no_source_diversity": {"source_diversity": False},
        "v3_optimized_no_applicability_diagnostics": {"applicability_diagnostics": False},
        "v3_optimized_no_context_budgeting_experimental": {"context_budgeting": False},
    }
    if mode != "v2_baseline":
        strategy = "v3_baseline" if mode == "v3_baseline" else "v3_optimized"
        options = ablations.get(mode, {})
        v3 = V3Retriever(db_path=v3_db, config=RetrievalConfig(strategy=strategy, **options))
    rows = []
    started = time.perf_counter()
    for query in chosen:
        case_started = time.perf_counter()
        if mode == "v2_baseline":
            evidence, diagnostics = retrieve_evidence_with_diagnostics(
                query["code"], version="v2", strategy="v2_baseline",
                language=query.get("language"),
            )
        else:
            evidence, diagnostics = v3.retrieve_with_diagnostics(
                query["code"], language=query.get("language"),
            )
        elapsed = time.perf_counter() - case_started
        families = Counter(str(item.source_family or "unknown") for item in evidence)
        parents = {str((item.metadata or {}).get("record_id") or item.evidence_id) for item in evidence}
        statuses = Counter(str((item.metadata or {}).get("language_match_status", "unavailable")) for item in evidence)
        tokens = int(diagnostics.get("evidence_tokens_used", 0)) if mode != "v2_baseline" else sum(
            max(1, (len(item.to_prompt_text().encode("utf-8")) + 2) // 3) for item in evidence
        )
        distances = [item.distance for item in evidence if item.distance is not None]
        rows.append({
            "query_id": query["query_id"], "split": query["split"],
            "retrieved_evidence_ids": [item.evidence_id for item in evidence],
            "source_families": dict(families), "parent_count": len(parents),
            "language_status_counts": dict(statuses), "distance_values": distances,
            "selected_count": len(evidence), "estimated_evidence_tokens": tokens,
            "token_budget_utilization": (tokens / diagnostics.get("retrieval_config", {}).get("evidence_token_budget", 1600)
                                         if mode != "v2_baseline" else None),
            "latency_seconds": elapsed,
            "retrieval_diagnostics": diagnostics,
            "relevance_metrics": {"precision_at_k": None, "recall_at_k": None, "mrr": None, "ndcg_at_k": None,
                                  "status": "unavailable_no_verified_human_judgments"},
        })
    return {
        "evaluation_version": "retrieval-diagnostics-v1", "mode": mode,
        "evidence_id_namespace": "v2 corpus IDs" if mode == "v2_baseline" else "v3 corpus IDs; never comparable by string alone",
        "v3_database": str(v3_db) if mode != "v2_baseline" else None,
        "query_count": len(rows), "query_ids": [row["query_id"] for row in rows],
        "unjudged": True, "retrieval_metrics": {
            "precision_at_k": None, "recall_at_k": None, "mrr": None, "ndcg_at_k": None,
            "reason": "No independently reviewed relevance judgments are available.",
        },
        "summary": {
            "empty_result_count": sum(row["selected_count"] == 0 for row in rows),
            "mean_selected_count": sum(row["selected_count"] for row in rows) / len(rows) if rows else None,
            "mean_latency_seconds": sum(row["latency_seconds"] for row in rows) / len(rows) if rows else None,
            "mean_token_utilization": (
                sum(row["token_budget_utilization"] for row in rows if row["token_budget_utilization"] is not None) /
                sum(row["token_budget_utilization"] is not None for row in rows)
                if any(row["token_budget_utilization"] is not None for row in rows) else None
            ),
        },
        "elapsed_seconds": time.perf_counter() - started, "cases": rows,
    }


def run_comparison(*, v3_db: Path, output_dir: Path = DEFAULT_OUTPUT,
                   include_validation: bool = True) -> dict[str, Any]:
    queries = _read_queries()
    expected = {"development", "validation"}
    if {query.get("split") for query in queries} != expected:
        raise ValueError("retrieval query set must contain development and validation splits")
    ids = [query.get("query_id") for query in queries]
    if len(ids) != len(set(ids)):
        raise ValueError("retrieval query IDs must be unique")
    modes = (
        "v2_baseline", "v3_baseline", "v3_optimized",
        "v3_optimized_no_parent_aware", "v3_optimized_no_language_aware",
        "v3_optimized_no_source_diversity", "v3_optimized_no_applicability_diagnostics",
        "v3_optimized_no_context_budgeting_experimental",
    )
    reports = {}
    for mode in modes:
        result = _run_one(mode, queries, v3_db=v3_db, include_validation=include_validation)
        path = output_dir / f"phase_e3_3c_{mode}_retrieval.json"
        _atomic_json(path, result)
        reports[mode] = {"path": str(path), "summary": result["summary"],
                         "retrieval_metrics": result["retrieval_metrics"]}
    combined = {"evaluation_version": "retrieval-diagnostics-v1", "modes": reports,
                "query_count": len(queries) if include_validation else sum(q["split"] == "development" for q in queries),
                "judgment_status": "unjudged_provisional", "same_query_set": True}
    _atomic_json(output_dir / "phase_e3_3c_retrieval_summary.json", combined)
    return combined


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v3-db", type=Path, default=PROJECT_ROOT / "rag" / "data" / "chroma_db_v3",
                        help="Existing validated v3 database or a consistent read-only copy")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--development-only", action="store_true")
    args = parser.parse_args()
    # Offline model loading is required; requests must not fetch model artifacts.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    result = run_comparison(v3_db=args.v3_db, output_dir=args.output_dir,
                            include_validation=not args.development_only)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
