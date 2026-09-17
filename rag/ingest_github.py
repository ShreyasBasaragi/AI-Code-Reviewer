import sys
import argparse
import logging
from typing import Dict, Any, List, Optional

import chromadb
from chromadb.utils import embedding_functions

from rag.github.client import GitHubClient, parse_repo_string
from rag.adapters.github_api import convert_github_api_pr_to_canonical
from rag.ingest import deduplicate_and_filter_records
from rag.adapters.base import CanonicalRecord
from rag.config import (
    CHROMA_PERSIST_DIR,
    COLLECTION_NAME,
    EMBEDDING_MODEL_NAME,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_chromadb_source_breakdown() -> Dict[str, int]:
    """Inspect ChromaDB collection and return document count by source tag."""
    try:
        client = chromadb.PersistentClient(path=str(CHROMA_PERSIST_DIR))
        collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=EMBEDDING_MODEL_NAME
            ),
            metadata={"hnsw:space": "cosine"}
        )
        if collection.count() == 0:
            return {}

        results = collection.get(include=["metadatas"])
        sources: Dict[str, int] = {}
        for m in results.get("metadatas", []):
            if m:
                s = m.get("source", "unknown")
                sources[s] = sources.get(s, 0) + 1
        return sources
    except Exception as e:
        logger.error(f"Error checking ChromaDB source breakdown: {e}")
        return {}


def upsert_github_records_to_chromadb(records: List[CanonicalRecord]) -> int:
    """Upsert new GitHub API canonical records idempotently into ChromaDB."""
    if not records:
        logger.warning("No records to upsert into ChromaDB.")
        return 0

    client = chromadb.PersistentClient(path=str(CHROMA_PERSIST_DIR))
    embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL_NAME
    )
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine"}
    )

    ids = [r.id for r in records]
    documents = [r.text for r in records]
    metadatas = [
        {
            "source": r.source,
            "language": r.language,
            "type": r.type,
            "tags": ",".join(r.tags),
            **{k: str(v) for k, v in r.metadata.items() if v is not None}
        }
        for r in records
    ]

    collection.upsert(
        ids=ids,
        documents=documents,
        metadatas=metadatas
    )

    logger.info(f"Successfully upserted {len(records)} GitHub API records into ChromaDB collection '{COLLECTION_NAME}'.")
    return len(records)


def ingest_github_repository(repo_str: str, pr_number: Optional[int] = None, max_prs: int = 5) -> Dict[str, Any]:
    """
    Main entry point for GitHub API live repository data ingestion.

    Args:
        repo_str: Repository in 'owner/repo' format.
        pr_number: Optional single PR number to ingest.
        max_prs: Maximum PRs to fetch if pr_number is not provided.
    """
    owner, repo = parse_repo_string(repo_str)
    client = GitHubClient()

    # Pre-ingestion ChromaDB breakdown check
    before_breakdown = get_chromadb_source_breakdown()

    logger.info(f"Connecting to GitHub REST API for target repository '{owner}/{repo}'...")

    raw_canonical_records: List[CanonicalRecord] = []
    total_files_fetched = 0
    total_reviews_fetched = 0
    total_comments_fetched = 0
    prs_processed = []

    if pr_number is not None:
        prs_to_fetch = [{"number": pr_number}]
    else:
        logger.info(f"Fetching recent closed PRs for repository '{owner}/{repo}' (max={max_prs})...")
        prs_to_fetch = client.get_closed_merged_prs(owner, repo, max_prs=max_prs)
        if not prs_to_fetch:
            logger.warning(f"No closed PRs found for repository '{owner}/{repo}'.")

    for pr in prs_to_fetch:
        num = pr.get("number")
        if not num:
            continue

        prs_processed.append(num)
        logger.info(f"Fetching details for PR #{num} from '{owner}/{repo}'...")

        pr_info = client.get_pull_request(owner, repo, num) or pr
        files = client.get_pr_files(owner, repo, num)
        reviews = client.get_pr_reviews(owner, repo, num)
        comments = client.get_pr_comments(owner, repo, num)

        total_files_fetched += len(files)
        total_reviews_fetched += len(reviews)
        total_comments_fetched += len(comments)

        recs = convert_github_api_pr_to_canonical(
            owner=owner,
            repo=repo,
            pr_info=pr_info,
            files=files,
            reviews=reviews,
            comments=comments
        )
        raw_canonical_records.extend(recs)

    # Data Quality Preprocessing & Deduplication
    accepted_records, filter_stats = deduplicate_and_filter_records(raw_canonical_records)

    # Upsert to ChromaDB without touching existing data
    records_upserted = upsert_github_records_to_chromadb(accepted_records)

    # Post-ingestion ChromaDB breakdown check
    after_breakdown = get_chromadb_source_breakdown()

    # Summary Report
    print("\n" + "=" * 65)
    print("        GITHUB API LIVE REPOSITORY INGESTION SUMMARY")
    print("=" * 65)
    print(f" Target Repository:          {owner}/{repo}")
    print(f" Pull Requests Ingested:     {prs_processed}")
    print(f" Changed Files Fetched:      {total_files_fetched}")
    print(f" PR Reviews Fetched:         {total_reviews_fetched}")
    print(f" Review Comments Fetched:    {total_comments_fetched}")
    print(f" Canonical Records Created:  {len(accepted_records)}")
    print(f" Records Upserted:           {records_upserted}")
    print("-" * 65)
    print(" ChromaDB Knowledge Base Preservation Verification:")
    all_sources = sorted(set(list(before_breakdown.keys()) + list(after_breakdown.keys())))
    for src in all_sources:
        cnt_before = before_breakdown.get(src, 0)
        cnt_after = after_breakdown.get(src, 0)
        diff = cnt_after - cnt_before
        diff_str = f" (+{diff} new)" if diff > 0 else " (Preserved)"
        print(f"   - {src:<25}: {cnt_after:<6}{diff_str}")
    print("=" * 65 + "\n")

    return {
        "repository": f"{owner}/{repo}",
        "prs_processed": prs_processed,
        "records_upserted": records_upserted,
        "before_breakdown": before_breakdown,
        "after_breakdown": after_breakdown
    }


def main():
    parser = argparse.ArgumentParser(
        description="Ingest live GitHub code review data into Critique RAG ChromaDB knowledge base."
    )
    parser.add_argument(
        "--repo",
        type=str,
        required=True,
        help="Target repository in format 'owner/repository' (e.g. 'microsoft/vscode')."
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="Optional specific pull request number to ingest (e.g. 12345)."
    )
    parser.add_argument(
        "--max-prs",
        type=int,
        default=5,
        help="Maximum PRs to fetch if --pr is omitted (default: 5)."
    )

    args = parser.parse_args()

    try:
        ingest_github_repository(repo_str=args.repo, pr_number=args.pr, max_prs=args.max_prs)
    except Exception as err:
        logger.error(f"GitHub API Ingestion failed: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
