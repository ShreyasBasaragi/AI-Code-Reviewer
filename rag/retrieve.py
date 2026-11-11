"""Read-only retrieval interface for the Critique RAG v2 vector database.

Public Interface Contract:
    def retrieve_context(code: str) -> list[str]:

Characteristics:
- Strictly read-only: never modifies the vector store, never triggers ingestion.
- Never creates collections: uses get_collection(); returns [] if unavailable.
- Up to 4 relevant context chunks returned, ordered from most to least relevant.
- Bounded and window-aware query handling for large code snippets.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import chromadb
from chromadb.utils import embedding_functions

from rag.config import (
    CHROMA_PERSIST_DIR,
    COLLECTION_NAME,
    DEFAULT_K,
    EMBEDDING_MODEL_NAME,
)

logger = logging.getLogger(__name__)

_chroma_client: Optional[chromadb.PersistentClient] = None
_collection = None
_embedding_function = None


def _get_collection():
    """
    Return the existing v2 collection; never create it during retrieval.
    Returns None gracefully if the database or collection does not exist.
    """
    global _chroma_client, _collection, _embedding_function

    if _collection is not None:
        return _collection

    if not CHROMA_PERSIST_DIR.exists():
        logger.warning(
            "ChromaDB persist directory '%s' does not exist. Run 'python -m rag.ingest' first.",
            CHROMA_PERSIST_DIR,
        )
        return None

    try:
        _chroma_client = chromadb.PersistentClient(path=str(CHROMA_PERSIST_DIR))
        _embedding_function = (
            embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=EMBEDDING_MODEL_NAME
            )
        )
        # get_collection is strict: raises if collection does not exist.
        _collection = _chroma_client.get_collection(
            name=COLLECTION_NAME,
            embedding_function=_embedding_function,
        )
        return _collection
    except Exception as exc:
        logger.warning(
            "RAG collection '%s' is unavailable. Run 'python -m rag.ingest' first. Details: %s",
            COLLECTION_NAME,
            exc,
        )
        return None


def _split_code_windows(code: str, max_chars: int = 800, stride_chars: int = 400) -> List[str]:
    """Split long code into overlapping windows for comprehensive retrieval."""
    lines = code.splitlines(keepends=True)
    if not lines:
        return [code]

    windows: List[str] = []
    current_chunk = []
    current_len = 0

    for line in lines:
        current_chunk.append(line)
        current_len += len(line)
        if current_len >= max_chars:
            windows.append("".join(current_chunk).strip())
            # Slide by roughly stride_chars
            retain_chars = max(0, current_len - stride_chars)
            new_chunk = []
            new_len = 0
            for l in reversed(current_chunk):
                if new_len + len(l) <= retain_chars:
                    new_chunk.insert(0, l)
                    new_len += len(l)
                else:
                    break
            current_chunk = new_chunk
            current_len = new_len

    if current_chunk:
        rem = "".join(current_chunk).strip()
        if rem and rem not in windows:
            windows.append(rem)

    return windows if windows else [code.strip()]


def retrieve_context(code: str) -> list[str]:
    """
    Return up to four most relevant context strings for a code snippet.

    Requirements:
    - Exact public interface: def retrieve_context(code: str) -> list[str]:
    - Most relevant results first.
    - Up to 4 results returned (DEFAULT_K = 4).
    - Returns [] for empty or invalid input.
    - Handles missing database gracefully.
    - Fully read-only: no collection creation, ingestion, or filesystem mutations.
    """
    if not isinstance(code, str) or not code.strip():
        return []

    collection = _get_collection()
    if collection is None:
        return []

    try:
        count = collection.count()
        if count == 0:
            logger.warning("RAG collection '%s' is empty.", COLLECTION_NAME)
            return []

        clean_code = code.strip()

        # If snippet is moderately sized (<= 1,200 chars), perform single bounded query
        if len(clean_code) <= 1200:
            results = collection.query(
                query_texts=[clean_code],
                n_results=min(DEFAULT_K, count),
            )
            raw_docs = (results.get("documents") or [[]])[0]
            # Deduplicate preserving order
            unique_docs: List[str] = []
            seen = set()
            for doc in raw_docs:
                if isinstance(doc, str) and doc.strip() and doc not in seen:
                    seen.add(doc)
                    unique_docs.append(doc)
            return unique_docs[:DEFAULT_K]

        # For larger code snippets, query across overlapping windows and rank by distance
        windows = _split_code_windows(clean_code)[:5]  # limit to top 5 windows
        query_results = collection.query(
            query_texts=windows,
            n_results=min(DEFAULT_K, count),
        )

        all_docs = query_results.get("documents") or []
        all_dists = query_results.get("distances") or []

        # Merge candidate documents by minimum distance
        doc_best_dist = {}
        for win_idx, docs in enumerate(all_docs):
            dists = all_dists[win_idx] if win_idx < len(all_dists) else [1.0] * len(docs)
            for doc, dist in zip(docs, dists):
                if isinstance(doc, str) and doc.strip():
                    if doc not in doc_best_dist or dist < doc_best_dist[doc]:
                        doc_best_dist[doc] = dist

        # Sort by distance (lower cosine distance = higher similarity)
        sorted_candidates = sorted(doc_best_dist.items(), key=lambda kv: kv[1])
        return [doc for doc, _ in sorted_candidates[:DEFAULT_K]]

    except Exception:
        logger.exception("Context retrieval encountered an unexpected error.")
        return []
