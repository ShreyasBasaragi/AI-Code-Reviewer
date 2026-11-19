"""Read-only retrieval interface for the Critique RAG v2 vector database.

Public Interface Contract:
    def retrieve_context(code: str) -> list[str]

Characteristics:
- Strictly read-only: never modifies the vector store, never triggers ingestion.
- Never creates collections: uses get_collection(); returns [] if unavailable.
- Uses only the configured v2 collection.
- Up to four relevant evidence records are returned, ordered by relevance.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

import chromadb
from chromadb.utils import embedding_functions

from rag.config import (
    CHROMA_PERSIST_DIR,
    COLLECTION_NAME,
    DEFAULT_K,
    EMBEDDING_MODEL_NAME,
)
from review_schema import RetrievedEvidence

logger = logging.getLogger(__name__)

_chroma_client: Optional[chromadb.PersistentClient] = None
_collection = None
_embedding_function = None


def _get_collection():
    """Return the existing v2 collection without creating or modifying it."""
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
        _embedding_function = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=EMBEDDING_MODEL_NAME
        )
        _collection = _chroma_client.get_collection(
            name=COLLECTION_NAME,
            embedding_function=_embedding_function,
        )
        return _collection
    except Exception as exc:
        logger.warning(
            "RAG collection '%s' is unavailable. Run 'python -m rag.ingest' first. Error type: %s",
            COLLECTION_NAME,
            type(exc).__name__,
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
            retain_chars = max(0, current_len - stride_chars)
            new_chunk = []
            new_len = 0
            for current_line in reversed(current_chunk):
                if new_len + len(current_line) <= retain_chars:
                    new_chunk.insert(0, current_line)
                    new_len += len(current_line)
                else:
                    break
            current_chunk = new_chunk
            current_len = new_len

    if current_chunk:
        remainder = "".join(current_chunk).strip()
        if remainder and remainder not in windows:
            windows.append(remainder)
    return windows if windows else [code.strip()]


def _evidence_from_query_results(results, query_index: int = 0) -> list[RetrievedEvidence]:
    """Map Chroma's aligned result arrays to source-aware evidence records."""
    ids_by_query = results.get("ids") or []
    docs_by_query = results.get("documents") or []
    metadata_by_query = results.get("metadatas") or []
    distances_by_query = results.get("distances") or []

    ids = ids_by_query[query_index] if query_index < len(ids_by_query) else []
    docs = docs_by_query[query_index] if query_index < len(docs_by_query) else []
    metadatas = metadata_by_query[query_index] if query_index < len(metadata_by_query) else []
    distances = distances_by_query[query_index] if query_index < len(distances_by_query) else []

    evidence = []
    for index, text in enumerate(docs):
        if not isinstance(text, str) or not text.strip():
            continue
        metadata = (
            metadatas[index]
            if index < len(metadatas) and isinstance(metadatas[index], dict)
            else {}
        )
        record_id = ids[index] if index < len(ids) and isinstance(ids[index], str) else None
        distance = distances[index] if index < len(distances) else None
        try:
            distance = float(distance) if distance is not None else None
        except (TypeError, ValueError):
            distance = None
        rule_identifier = metadata.get("rule_id") or metadata.get("cwe_id") or metadata.get("rule")
        evidence.append(
            RetrievedEvidence(
                evidence_id=record_id,
                source_family=metadata.get("source"),
                source_url=metadata.get("source_url"),
                rule_identifier=str(rule_identifier) if rule_identifier is not None else None,
                text=text,
                language=metadata.get("language"),
                metadata=metadata,
                distance=distance,
            )
        )
    return evidence


def _retrieve_evidence_v2(code: str, *, strict: bool = False) -> list[RetrievedEvidence]:
    """Return structured evidence from the existing v2 collection only.

    This function is read-only, uses the configured collection, and does not create
    collections or load model weights. Query embeddings are generated by Chroma's
    configured embedding function when this function is called.
    """
    if not isinstance(code, str) or not code.strip():
        return []
    collection = _get_collection()
    if collection is None:
        if strict:
            from rag.retrieve_v3 import RAGConfigurationError

            raise RAGConfigurationError(
                "v2 Chroma collection is unavailable; check the configured database and retrieval warning"
            )
        return []

    try:
        count = collection.count()
        if count == 0:
            logger.warning("RAG collection '%s' is empty.", COLLECTION_NAME)
            if strict:
                from rag.retrieve_v3 import RAGConfigurationError

                raise RAGConfigurationError(f"v2 Chroma collection {COLLECTION_NAME!r} is empty")
            return []

        clean_code = code.strip()
        if len(clean_code) <= 1200:
            results = collection.query(
                query_texts=[clean_code],
                n_results=min(DEFAULT_K, count),
                include=["documents", "metadatas", "distances"],
            )
            return _evidence_from_query_results(results)

        windows = _split_code_windows(clean_code)[:5]
        query_results = collection.query(
            query_texts=windows,
            n_results=min(DEFAULT_K, count),
            include=["documents", "metadatas", "distances"],
        )
        candidates: dict[str, tuple[float, RetrievedEvidence]] = {}
        for window_index in range(len(windows)):
            for item in _evidence_from_query_results(query_results, window_index):
                key = item.evidence_id or item.text
                distance = item.distance if item.distance is not None else float("inf")
                if key not in candidates or distance < candidates[key][0]:
                    candidates[key] = (distance, item)
        ordered = sorted(candidates.values(), key=lambda pair: pair[0])
        return [item for _, item in ordered[:DEFAULT_K]]
    except Exception as exc:
        logger.warning(
            "Structured context retrieval encountered an error (%s).",
            type(exc).__name__,
        )
        if strict:
            from rag.retrieve_v3 import RAGConfigurationError

            if isinstance(exc, RAGConfigurationError):
                raise
            raise RAGConfigurationError(
                f"v2 Chroma retrieval failed: {type(exc).__name__}"
            ) from exc
        return []


def retrieve_evidence(code: str, *, version: str | None = None,
                      strategy: str | None = None, language: str | None = None,
                      file_path: str | None = None) -> list[RetrievedEvidence]:
    """Retrieve evidence from the explicitly selected corpus version.

    Calls without options preserve the historical v2 behavior. Set
    ``AI_CODE_REVIEWER_RAG_VERSION=v3`` to explicitly opt into v3; a missing v3
    corpus/model raises an actionable error and never falls back to v2.
    """
    from rag.retrieve_v3 import retrieval_backend_selection

    selected_version, selected_strategy = retrieval_backend_selection(version, strategy)
    if selected_version == "v2":
        return _retrieve_evidence_v2(code)
    from rag.retrieve_v3 import retrieve_evidence_v3
    return retrieve_evidence_v3(code, strategy=selected_strategy,
                                language=language, file_path=file_path)


def retrieve_evidence_with_diagnostics(
    code: str, *, version: str | None = None, strategy: str | None = None,
    language: str | None = None, file_path: str | None = None,
) -> tuple[list[RetrievedEvidence], dict]:
    """Return evidence plus optional per-call diagnostics without changing callers."""
    from rag.retrieve_v3 import retrieval_backend_selection

    selected_version, selected_strategy = retrieval_backend_selection(version, strategy)
    if selected_version == "v3":
        from rag.retrieve_v3 import _DEFAULT_V3, RetrievalConfig, V3Retriever
        key = (str(__import__("rag.config", fromlist=["PROJECT_ROOT"]).PROJECT_ROOT /
                    "rag" / "data" / "chroma_db_v3"), selected_strategy)
        retriever = _DEFAULT_V3.setdefault(
            key, V3Retriever(config=RetrievalConfig(strategy=selected_strategy))
        )
        return retriever.retrieve_with_diagnostics(code, language=language, file_path=file_path)
    started = time.perf_counter()
    try:
        evidence = _retrieve_evidence_v2(code, strict=True)
    except Exception as exc:
        return [], {
            "corpus_version": "v2", "strategy": "v2_baseline",
            "status": "errored", "error_type": type(exc).__name__,
            "selected_count": 0, "candidate_count": 0,
            "source_distribution": {}, "language_distribution": {},
            "retrieval_seconds": time.perf_counter() - started,
        }
    diagnostics = {
        "corpus_version": "v2", "strategy": "v2_baseline",
        "status": "passed",
        "selected_count": len(evidence),
        "candidate_count": len(evidence),
        "source_distribution": {}, "language_distribution": {},
        "retrieval_seconds": time.perf_counter() - started,
        "note": "The v2 collection does not persist the full v3 parent/language metadata contract.",
    }
    return evidence, diagnostics


def retrieve_context(code: str) -> list[str]:
    """Backward-compatible text-only retrieval interface."""
    contexts = []
    seen = set()
    evidence = retrieve_evidence(code)
    for item in evidence:
        if item.text not in seen:
            seen.add(item.text)
            contexts.append(item.text)
    return contexts[:DEFAULT_K]
