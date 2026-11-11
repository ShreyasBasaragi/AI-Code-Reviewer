"""Explicit corpus builder for the versioned Critique RAG v2 index.

Builds a reproducible, lightweight ChromaDB knowledge base containing:
  1. Historical code reviews (Microsoft CodeReviewer Comment_Generation training split)
  2. Security patterns (MITRE CWE + OWASP Cheat Sheets)
  3. Style & static-analysis rules (PEP 8 + Google Python Style Guide + Bandit)

Total target chunks: 6,000–12,000
Database size target: < 50 MB
Embedding model: all-MiniLM-L6-v2 (256-token maximum input length)

Usage:
    python scripts/fetch_reference_sources.py
    python scripts/fetch_microsoft_codereviewer.py
    python -m rag.ingest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

import chromadb
from chromadb.utils import embedding_functions
from transformers import AutoTokenizer

from rag.adapters.base import (
    CanonicalRecord,
    compute_content_hash,
    sanitize_text,
    validate_canonical_record,
)
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
MAX_CHUNK_TOKENS = 200
CHUNK_OVERLAP_TOKENS = 20
BATCH_SIZE = 256

# Corpus category quotas
MIN_MICROSOFT_CHUNKS = 5_000
MAX_MICROSOFT_CHUNKS = 10_000
TARGET_MICROSOFT_CHUNKS = 5_200

MIN_SECURITY_CHUNKS = 500
MAX_SECURITY_CHUNKS = 1_000
TARGET_SECURITY_CHUNKS = 550

MIN_STYLE_CHUNKS = 300
MAX_STYLE_CHUNKS = 500
TARGET_STYLE_CHUNKS = 350

TARGET_MIN_CHUNKS = 6_000
MAX_TOTAL_CHUNKS = 12_000
MAX_DB_BYTES = 50 * 1024 * 1024  # 50 MB target


def _load_tokenizer():
    logger.info("Loading embedding tokenizer: %s", TOKENIZER_MODEL_NAME)
    return AutoTokenizer.from_pretrained(TOKENIZER_MODEL_NAME)


def deduplicate_and_filter_records(
    records: List[CanonicalRecord],
) -> Tuple[List[CanonicalRecord], Dict[str, int]]:
    """
    Validate and deduplicate canonical records by normalized content hash.
    Exported for backward compatibility with live ingestion and testing pipelines.
    """
    accepted: List[CanonicalRecord] = []
    seen = set()
    rejected = 0
    deduplicated = 0

    for record in records:
        record.text = sanitize_text(record.text)
        if not validate_canonical_record(record):
            rejected += 1
            continue

        digest = compute_content_hash(record.text)
        if digest in seen:
            deduplicated += 1
            continue
        seen.add(digest)
        accepted.append(record)

    return accepted, {
        "records_rejected": rejected,
        "records_deduplicated": deduplicated,
    }


def chunk_record(
    record: CanonicalRecord,
    tokenizer,
    max_tokens: int = MAX_CHUNK_TOKENS,
    overlap: int = CHUNK_OVERLAP_TOKENS,
) -> List[CanonicalRecord]:
    """
    Split a source record into deterministic, model-token-bounded chunks.
    Preserves parent ID, chunk index, and source metadata.
    """
    text = sanitize_text(record.text)
    token_ids = tokenizer.encode(text, add_special_tokens=False)
    if not token_ids:
        return []

    # If within limit, return single complete chunk
    if len(token_ids) <= max_tokens:
        return [
            CanonicalRecord(
                id=f"{record.id}_chunk_0",
                text=text,
                source=record.source,
                language=record.language,
                type=record.type,
                tags=list(record.tags),
                metadata={
                    **record.metadata,
                    "parent_id": record.id,
                    "chunk_index": "0",
                    "chunk_tokens": str(len(token_ids)),
                },
            )
        ]

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


def check_holdout_exclusions(
    records: List[CanonicalRecord],
    manifest_path: Path = Path("rag/data/evaluation/heldout_manifest.json"),
) -> Tuple[List[CanonicalRecord], Dict[str, Any]]:
    """
    Filter candidate records against the holdout exclusion manifest.
    Rejects exact matching held-out examples by fingerprint, identifier, or text overlap.
    """
    if not manifest_path.exists():
        logger.warning("Holdout manifest not found at %s. Verification pending.", manifest_path)
        return records, {
            "excluded_count": 0,
            "holdout_verified": False,
            "status": "manifest_missing",
        }

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read holdout manifest: %s", exc)
        return records, {
            "excluded_count": 0,
            "holdout_verified": False,
            "status": "read_error",
        }

    fingerprints = set(manifest.get("fingerprints", []))
    normalized_texts = manifest.get("normalized_texts", [])
    official_verified = bool(manifest.get("official_holdout_verified", False))
    status = manifest.get("status", "pending_official_list")

    allowed_records = []
    excluded_count = 0

    for rec in records:
        # 1. Match against known fingerprints
        c_hash = compute_content_hash(rec.text)
        if c_hash in fingerprints or rec.id in fingerprints:
            excluded_count += 1
            continue

        # 2. Match metadata original_id
        orig_id = str(rec.metadata.get("original_id") or "")
        if orig_id and orig_id in fingerprints:
            excluded_count += 1
            continue

        # 3. Content overlap check against holdout snippets
        norm_rec_text = " ".join(rec.text.split()).casefold()
        has_overlap = False
        for target_snippet in normalized_texts:
            if len(target_snippet) >= 60 and target_snippet in norm_rec_text:
                has_overlap = True
                break
        if has_overlap:
            excluded_count += 1
            continue

        allowed_records.append(rec)

    logger.info(
        "Holdout exclusion check: %d candidate records evaluated, %d excluded. Status: %s",
        len(records),
        excluded_count,
        status,
    )
    return allowed_records, {
        "excluded_count": excluded_count,
        "holdout_verified": official_verified,
        "status": status,
    }


def prepare_records(records: List[CanonicalRecord], tokenizer) -> Tuple[List[CanonicalRecord], Dict[str, int]]:
    """Validate, chunk, and deduplicate records by normalized content and unique ID."""
    chunks = []
    seen_hashes = set()
    seen_ids = set()
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
            if digest in seen_hashes:
                duplicates += 1
                continue
            seen_hashes.add(digest)

            # Guarantee ID is globally unique across all chunks
            if chunk.id in seen_ids:
                chunk.id = f"{chunk.id}_{len(chunks)}"
            seen_ids.add(chunk.id)

            chunks.append(chunk)

    return chunks, {"rejected": rejected, "deduplicated": duplicates}


def _metadata(record: CanonicalRecord) -> Dict[str, str]:
    """Extract compact metadata dictionary to minimize SQLite index storage overhead."""
    values = {
        "source": str(record.source),
        "language": str(record.language),
        "type": str(record.type),
    }
    for key in ("project", "category", "cwe_id", "weakness_name", "cheatsheet", "rule"):
        val = record.metadata.get(key)
        if val is not None and isinstance(val, (str, int, float, bool)):
            values[key] = str(val)[:60]
    return values


def _directory_size(path: Path) -> int:
    return sum(file.stat().st_size for file in path.rglob("*") if file.is_file())


def ingest_to_chromadb(records: List[CanonicalRecord]) -> int:
    """
    Index records into the versioned v2 ChromaDB collection.
    The legacy collection and database directory are never touched.
    """
    if not records:
        raise RuntimeError("No valid corpus chunks were produced; refusing to create an empty index.")

    CHROMA_PERSIST_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_PERSIST_DIR))

    existing = {item if isinstance(item, str) else item.name for item in client.list_collections()}
    if COLLECTION_NAME in existing:
        logger.info("Replacing existing collection '%s'.", COLLECTION_NAME)
        client.delete_collection(name=COLLECTION_NAME)

    embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL_NAME
    )
    collection = client.create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine", "corpus_version": "2"},
    )

    total_chunks = len(records)
    for offset in range(0, total_chunks, BATCH_SIZE):
        batch = records[offset : offset + BATCH_SIZE]
        collection.add(
            ids=[r.id for r in batch],
            documents=[r.text for r in batch],
            metadatas=[_metadata(r) for r in batch],
        )
        if (offset + len(batch)) % 1024 == 0 or (offset + len(batch)) == total_chunks:
            logger.info("Indexed %d/%d chunks into ChromaDB", offset + len(batch), total_chunks)

    # Perform SQLite vacuum to reclaim freelist pages
    sqlite_file = CHROMA_PERSIST_DIR / "chroma.sqlite3"
    if sqlite_file.exists():
        try:
            import sqlite3
            con = sqlite3.connect(str(sqlite_file))
            con.execute("VACUUM;")
            con.close()
        except Exception as e:
            logger.warning("SQLite vacuum error: %s", e)

    actual_count = collection.count()
    if actual_count != total_chunks:
        raise RuntimeError(
            f"Stored count mismatch: expected {total_chunks}, got {actual_count} in ChromaDB."
        )
    return actual_count


def run_ingestion(max_ms_records: int = 8000) -> Dict[str, Any]:
    """
    Build the Critique RAG v2 corpus and persist to ChromaDB.
    Enforces strict category quotas and database size limits.
    """
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHROMA_PERSIST_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Load source records from approved 3 categories only
    logger.info("Loading Microsoft CodeReviewer training records...")
    microsoft_records = load_microsoft_codereviewer(max_per_category=max_ms_records)
    logger.info("Loaded %d Microsoft source records.", len(microsoft_records))

    logger.info("Loading reference rule records (CWE, OWASP, PEP 8, Google, Bandit)...")
    reference_records = load_reference_rule_records()
    logger.info("Loaded %d reference rule records.", len(reference_records))

    all_source_records = microsoft_records + reference_records

    # 2. Check holdout exclusions
    manifest_file = PROCESSED_DATA_DIR.parent / "evaluation" / "heldout_manifest.json"
    allowed_records, holdout_info = check_holdout_exclusions(all_source_records, manifest_file)

    # 3. Tokenize, chunk, and deduplicate
    tokenizer = _load_tokenizer()
    chunks, quality = prepare_records(allowed_records, tokenizer)

    # 4. Enforce category quotas
    microsoft_chunks = [c for c in chunks if c.source == "microsoft_codereviewer"][:TARGET_MICROSOFT_CHUNKS]
    security_chunks = [c for c in chunks if c.source == "owasp_cwe"][:TARGET_SECURITY_CHUNKS]
    style_chunks = [c for c in chunks if c.source == "style_static_rules"][:TARGET_STYLE_CHUNKS]

    # Validate category counts
    ms_count = len(microsoft_chunks)
    sec_count = len(security_chunks)
    style_count = len(style_chunks)
    total_chunks = ms_count + sec_count + style_count

    chunks = microsoft_chunks + security_chunks + style_chunks
    source_counts = {
        "microsoft_codereviewer": ms_count,
        "owasp_cwe": sec_count,
        "style_static_rules": style_count,
    }

    ms_valid = MIN_MICROSOFT_CHUNKS <= ms_count <= MAX_MICROSOFT_CHUNKS
    sec_valid = MIN_SECURITY_CHUNKS <= sec_count <= MAX_SECURITY_CHUNKS
    style_valid = MIN_STYLE_CHUNKS <= style_count <= MAX_STYLE_CHUNKS
    total_valid = TARGET_MIN_CHUNKS <= total_chunks <= MAX_TOTAL_CHUNKS
    category_quotas_met = ms_valid and sec_valid and style_valid and total_valid

    logger.info(
        "Corpus validation: MS=%d (%s), Security=%d (%s), Style=%d (%s), Total=%d (%s)",
        ms_count, "OK" if ms_valid else "FAIL",
        sec_count, "OK" if sec_valid else "FAIL",
        style_count, "OK" if style_valid else "FAIL",
        total_chunks, "OK" if total_valid else "FAIL",
    )

    if not category_quotas_met:
        error_msg = (
            f"Corpus category quota violation: MS={ms_count} (req {MIN_MICROSOFT_CHUNKS}-{MAX_MICROSOFT_CHUNKS}), "
            f"Security={sec_count} (req {MIN_SECURITY_CHUNKS}-{MAX_SECURITY_CHUNKS}), "
            f"Style={style_count} (req {MIN_STYLE_CHUNKS}-{MAX_STYLE_CHUNKS}), "
            f"Total={total_chunks} (req {TARGET_MIN_CHUNKS}-{MAX_TOTAL_CHUNKS})."
        )
        logger.error(error_msg)
        raise ValueError(error_msg)

    # 5. Save canonical chunks JSONL artifact
    processed_path = PROCESSED_DATA_DIR / "canonical_chunks.jsonl"
    with processed_path.open("w", encoding="utf-8") as out_f:
        for item in chunks:
            out_f.write(json.dumps(item.to_dict(), ensure_ascii=False) + "\n")
    logger.info("Saved %d canonical chunks to %s", len(chunks), processed_path)

    # 6. Index into ChromaDB
    indexed = ingest_to_chromadb(chunks)
    db_size = _directory_size(CHROMA_PERSIST_DIR)
    db_mb = round(db_size / (1024 * 1024), 2)
    db_under_50mb = db_size < MAX_DB_BYTES

    build_success = category_quotas_met and db_under_50mb and (indexed == total_chunks)

    report = {
        "corpus_version": "2",
        "collection_name": COLLECTION_NAME,
        "embedding_model": EMBEDDING_MODEL_NAME,
        "source_records": len(all_source_records),
        "chunks_indexed": indexed,
        "chunks_by_source": source_counts,
        "rejected_records": quality["rejected"],
        "deduplicated_chunks": quality["deduplicated"],
        "excluded_holdout_records": holdout_info["excluded_count"],
        "database_bytes": db_size,
        "database_mb": db_mb,
        "database_under_50mb": db_under_50mb,
        "target_chunk_range_met": total_valid,
        "category_quotas_met": category_quotas_met,
        "holdout_verified": holdout_info["holdout_verified"],
        "official_holdout_status": holdout_info["status"],
        "build_success": build_success,
    }

    report_path = PROCESSED_DATA_DIR / "ingestion_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print("\n" + "=" * 65)
    print("             CRITIQUE RAG v2 INGESTION REPORT")
    print("=" * 65)
    print(f" Collection Name:          {COLLECTION_NAME}")
    print(f" Embedding Model:          {EMBEDDING_MODEL_NAME}")
    print(f" Total Chunks Indexed:     {indexed:,}")
    print(f" Chunks by Category:")
    print(f"   - Microsoft Reviews:    {ms_count:,} (target: 5,000–10,000)")
    print(f"   - Security (CWE+OWASP): {sec_count:,} (target: 500–1,000)")
    print(f"   - Style & Static Rules: {style_count:,} (target: 300–500)")
    print(f" Category Quotas Met:      {category_quotas_met}")
    print(f" Rejected Records:         {quality['rejected']}")
    print(f" Deduplicated Chunks:      {quality['deduplicated']}")
    print(f" Excluded Holdout Records: {holdout_info['excluded_count']}")
    print(f" Database Disk Size:       {db_mb} MB (target: < 50 MB)")
    print(f" Database Under 50 MB:     {db_under_50mb}")
    print(f" Official Holdout Status:  {holdout_info['status']}")
    print(f" Build Successful:         {build_success}")
    print("=" * 65 + "\n")

    if not db_under_50mb:
        raise RuntimeError(f"Database size {db_mb} MB exceeds the 50 MB requirement.")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the Critique RAG v2 index.")
    parser.add_argument(
        "--max-ms-records",
        type=int,
        default=8000,
        help="Maximum training records from Microsoft Comment_Generation.",
    )
    args = parser.parse_args()
    run_ingestion(max_ms_records=args.max_ms_records)
