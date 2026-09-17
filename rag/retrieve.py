import logging
from typing import List, Optional, Dict, Any

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


def _get_collection():
    """Lazy initialize and return ChromaDB collection instance."""
    global _chroma_client, _collection

    if _collection is None:
        try:
            _chroma_client = chromadb.PersistentClient(path=str(CHROMA_PERSIST_DIR))
            embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=EMBEDDING_MODEL_NAME
            )
            _collection = _chroma_client.get_or_create_collection(
                name=COLLECTION_NAME,
                embedding_function=embedding_fn,
                metadata={"hnsw:space": "cosine"}
            )
        except Exception as e:
            logger.error(f"Failed to initialize ChromaDB collection: {e}")
            return None

    return _collection


def retrieve_context_for_repository(
    code: str,
    repository: Optional[str] = None,
    k: int = DEFAULT_K
) -> List[str]:
    """
    Retrieve top-k relevant context strings for a given target code snippet,
    with optional repository-specific metadata filtering.

    Args:
        code: Input source code snippet or PR diff string.
        repository: Optional target repository string (e.g. 'psf/requests').
        k: Number of relevant context snippets to retrieve.

    Returns:
        List of context text strings matching the query snippet.
    """
    if not code or not code.strip():
        logger.warning("Empty code string passed to retrieval function.")
        return []

    collection = _get_collection()
    if collection is None:
        logger.error("ChromaDB collection unavailable.")
        return []

    # Limit code query string length to avoid token limits
    query_text = code.strip()[:2000]

    try:
        count = collection.count()
        if count == 0:
            logger.warning("ChromaDB collection is empty. Run ingestion first.")
            return []

        actual_k = min(k, count)
        where_filter = None
        if repository:
            where_filter = {"repository": str(repository)}

        query_params: Dict[str, Any] = {
            "query_texts": [query_text],
            "n_results": actual_k
        }
        if where_filter:
            query_params["where"] = where_filter

        try:
            results = collection.query(**query_params)
        except Exception as filter_err:
            logger.warning(f"Repository filter failed or returned no hits: {filter_err}. Falling back to global search.")
            results = collection.query(query_texts=[query_text], n_results=actual_k)

        documents = results.get("documents", [[]])[0]
        return documents if documents else []

    except Exception as e:
        logger.error(f"Error querying ChromaDB context: {e}")
        return []


def retrieve_context(code: str, k: int = DEFAULT_K) -> List[str]:
    """
    Retrieve top-k relevant context strings (style rules, patterns, past reviews)
    given a target code snippet or PR diff.

    FROZEN INTERFACE CONTRACT:
    Signature MUST remain `retrieve_context(code: str, k: int = 5) -> list[str]`.

    Args:
        code: Input source code snippet or PR diff string.
        k: Number of relevant context snippets to retrieve.

    Returns:
        A list of context text strings matching the query snippet.
    """
    return retrieve_context_for_repository(code=code, repository=None, k=k)
