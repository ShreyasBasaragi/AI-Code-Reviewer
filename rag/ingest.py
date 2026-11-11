"""Explicit corpus builder for the versioned RAG index.

Run after downloading official reference sources:
    python scripts/fetch_reference_sources.py
    python -m rag.ingest

The Hugging Face github-codereview dataset is deliberately NOT an ingestion
source. Its test split is reserved for the independent evaluation holdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

import chromadb
from chromadb.utils import embedding_functions
from transformers import AutoTokenizer

from rag.adapters.base import CanonicalRecord, sanitize_text, validate_canonical_record
from rag.adapters.microsoft_codereviewer import load_microsoft_codereviewer
from rag.adapters.reference_rules import load_reference_rule_records
from rag.config import (
    CHROMA_PERSIST_DIR,
    COLLECTION_NAME,
    EMBEDDING_MODEL_NAME,
    PROCESSED_DATA_DIR,
    RAW_DATA_DIR,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TOKENIZER_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
MAX_CHUNK_TOKENS = 220
CHUNK_OVERLAP_TOKENS = 24
BATCH_SIZE = 256
MAX_TOTAL_CHUNKS = 12_000
TARGET_MIN_CHUNKS = 6_000
MAX_DB_BYTES = 50 * 1024 * 1024


def _load_tokenizer():
    logger.info("Loading tokenizer: %s", TOKENIZER_MODEL_NAME)
    return AutoTokenizer.from_pretrained(TOKENIZER_MODEL_NAME)


def chunk_record(
    record: CanonicalRecord,
    tokenizer,
    max_tokens: int = MAX_CHUNK_TOKENS,
    overlap: int = CHUNK_OVERLAP_TOKENS,
) -> list[CanonicalRecord]:
    """Split a source record into deterministic, model-token-bounded chunks."""
    text = sanitize_text(record.text)
    token_ids = tokenizer.encode(text, add_special_tokens=False)
    if not token_ids:
        return []

    chunks = []
    start = 0
    chunk_index = 0
    while start < len(token_ids):
        end = min(start + max_tokens, len(token_ids))
        piece = tokenizer.decode(
            token_ids[start:end],
            skip_special_tokens=True,
            clean_up_tokenization_spaces=False,
        ).strip()
        if piece:
            chunks.append(
                CanonicalRecord(
                    id=f"{record.id}_chunk_{chunk_index}",
                    text=piece,
                    source=record.source,
                    language=record.language,
                    type=record.type,
                    tags=list(record.tags),
                    metadata={
                        **record.metadata,
                        "parent_id": record.id,
                        "chunk_index": str(chunk_index),
                        "chunk_tokens": str(end - start),
                    },
                )
            )
            chunk_index += 1

        if end >= len(token_ids):
            break
        start = end - overlap

    return chunks


def prepare_records(records: list[CanonicalRecord], tokenizer):
    """Validate, chunk, and deduplicate records by normalized content."""
    chunks = []
    seen = set()
    rejected = 0
    duplicates = 0

    for record in records:
        record.text = sanitize_text(record.text)
        if not validate_canonical_record(record):
            rejected += 1
            continue

        for chunk in chunk_record(record, tokenizer):
            normalized = " ".join(chunk.text.split()).casefold()
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            if digest in seen:
                duplicates += 1
                continue
            seen.add(digest)
            chunks.append(chunk)

    return chunks, {"rejected": rejected, "deduplicated": duplicates}


def _metadata(record: CanonicalRecord) -> dict[str, str]:
    values = {
        "source": record.source,
        "language": record.language,
        "type": record.type,
        "tags": ",".join(record.tags),
    }
    values.update(
        {
            str(key): str(value)
            for key, value in record.metadata.items()
            if value is not None and isinstance(value, (str, int, float, bool))
        }
    )
    return values


def _directory_size(path: Path) -> int:
    return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())


def ingest_to_chromadb(records: list[CanonicalRecord]) -> int:
    """Rebuild only the v2 collection; leave the legacy collection untouched."""
    if not records:
        raise RuntimeError("No valid corpus chunks were produced; refusing to create an empty index.")

    CHROMA_PERSIST_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_PERSIST_DIR))
    existing = {item.name for item in client.list_collections()}
    if COLLECTION_NAME in existing:
        logger.warning("Replacing versioned collection '%s'.", COLLECTION_NAME)
        client.delete_collection(name=COLLECTION_NAME)

    embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL_NAME
    )
    collection = client.create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine", "corpus_version": "2"},
    )

    for offset in range(0, len(records), BATCH_SIZE):
        batch = records[offset : offset + BATCH_SIZE]
        collection.add(
            ids=[record.id for record in batch],
            documents=[record.text for record in batch],
            metadatas=[_metadata(record) for record in batch],
        )
        logger.info("Indexed %s/%s chunks", min(offset + len(batch), len(records)), len(records))

    return collection.count()


def run_ingestion(max_ms_per_category: int = 8000) -> dict[str, Any]:
    """Build the v2 corpus from Microsoft training data and official rule sources."""
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHROMA_PERSIST_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("Loading Microsoft CodeReviewer training records...")
    microsoft_records = load_microsoft_codereviewer(
        max_per_category=max_ms_per_category
    )
    logger.info("Loaded %s Microsoft source records.", len(microsoft_records))

    logger.info("Loading OWASP/CWE and style/static-analysis records...")
    reference_records = load_reference_rule_records()
    logger.info("Loaded %s reference-rule records.", len(reference_records))

    source_records = microsoft_records + reference_records
    tokenizer = _load_tokenizer()
    chunks, quality = prepare_records(source_records, tokenizer)

    # Bound the total corpus without changing the security/style quotas.
    if len(chunks) > MAX_TOTAL_CHUNKS:
        microsoft_chunks = [item for item in chunks if item.source == "microsoft_codereviewer"]
        rule_chunks = [item for item in chunks if item.source != "microsoft_codereviewer"]
        room_for_ms = max(0, MAX_TOTAL_CHUNKS - len(rule_chunks))
        chunks = rule_chunks + microsoft_chunks[:room_for_ms]

    source_counts = dict(Counter(item.source for item in chunks))
    total = len(chunks)
    logger.info("Prepared %s chunks: %s", total, source_counts)

    if total < TARGET_MIN_CHUNKS:
        logger.warning(
            "Corpus currently has %s chunks; teammate target is %s–%s. "
            "Check Microsoft source archive coverage and rule-source extraction.",
            total,
            TARGET_MIN_CHUNKS,
            MAX_TOTAL_CHUNKS,
        )

    processed_path = PROCESSED_DATA_DIR / "canonical_chunks.jsonl"
    with processed_path.open("w", encoding="utf-8") as output:
        for item in chunks:
            output.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")

    indexed = ingest_to_chromadb(chunks)
    db_size = _directory_size(CHROMA_PERSIST_DIR)

    report = {
        "source_records": len(source_records),
        "chunks_indexed": indexed,
        "chunks_by_source": source_counts,
        "rejected_records": quality["rejected"],
        "deduplicated_chunks": quality["deduplicated"],
        "database_bytes": db_size,
        "database_mb": round(db_size / (1024 * 1024), 2),
        "database_under_50mb": db_size < MAX_DB_BYTES,
        "target_chunk_range_met": TARGET_MIN_CHUNKS <= indexed <= MAX_TOTAL_CHUNKS,
    }
    report_path = PROCESSED_DATA_DIR / "ingestion_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\nRAG v2 ingestion report")
    print(json.dumps(report, indent=2))
    if db_size >= MAX_DB_BYTES:
        logger.warning("ChromaDB directory exceeds the 50 MB target.")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the Critique RAG v2 index.")
    parser.add_argument(
        "--max-ms-per-category",
        type=int,
        default=8000,
        help="Maximum training records per Microsoft dataset category.",
    )
    args = parser.parse_args()
    run_ingestion(max_ms_per_category=args.max_ms_per_category)
