"""Read-only retrieval for the versioned RAG corpus.

Retrieval never creates a ChromaDB collection or triggers ingestion. Build the
collection explicitly with the ingestion command before calling this module.
"""
import logging
from typing import List, Optional

import chromadb
from chromadb.utils import embedding_functions

from rag.config import (
    CHROMA_PERSIST_DIR,
    COLLECTION_NAME,
    EMBEDDING_MODEL_NAME,
    DEFAULT_K,
)

logger = logging.getLogger(__name__)

_chroma_client: Optional[chromadb.PersistentClient] = None
_collection = None
_embedding_function = None


def _get_collection():
    """Return the existing v2 collection; never create it during retrieval."""
    global _chroma_client, _collection, _embedding_function

    if _collection is not None:
        return _collection

    try:
        _chroma_client = chromadb.PersistentClient(path=str(CHROMA_PERSIST_DIR))
        _embedding_function = (
            embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=EMBEDDING_MODEL_NAME
            )
        )
        # get_collection is intentional: a missing index is an actionable setup
        # error, not a reason for a read operation to mutate local storage.
        _collection = _chroma_client.get_collection(
            name=COLLECTION_NAME,
            embedding_function=_embedding_function,
        )
        return _collection
    except Exception as exc:
        logger.warning(
            "RAG collection '%s' is unavailable. Run the explicit ingestion "
            "command first. Details: %s",
            COLLECTION_NAME,
            exc,
        )
        return None


def retrieve_context(code: str) -> list[str]:
    """Return the three most relevant context chunks for a code snippet.

    This is the stable integration interface. It returns up to three chunks and performs read-only retrieval
    and has no ingestion, collection-creation, or filesystem side effects.
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

        # Keep the query bounded. The embedding model also applies its own
        # tokenizer truncation; corpus chunks are bounded during ingestion.
        query_text = code.strip()[:6000]
        results = collection.query(
            query_texts=[query_text],
            n_results=min(DEFAULT_K, count),
        )
        documents = results.get("documents") or [[]]
        return [doc for doc in documents[0] if isinstance(doc, str)]
    except Exception:
        logger.exception("Context retrieval failed.")
        return []
