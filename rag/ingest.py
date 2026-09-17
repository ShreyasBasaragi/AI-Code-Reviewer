import json
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Any, Tuple

import chromadb
from chromadb.utils import embedding_functions

from rag.adapters.base import CanonicalRecord, sanitize_text, validate_canonical_record
from rag.adapters.github_codereview import load_github_codereview
from rag.adapters.microsoft_codereviewer import load_microsoft_codereviewer
from rag.config import (
    RAW_DATA_DIR,
    PROCESSED_DATA_DIR,
    CHROMA_PERSIST_DIR,
    COLLECTION_NAME,
    EMBEDDING_MODEL_NAME,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def load_raw_baseline_chunks() -> List[CanonicalRecord]:
    """Load baseline style guide & anti-pattern chunks from raw data directory."""
    baseline_records: List[CanonicalRecord] = []

    # Style Guide
    style_file = RAW_DATA_DIR / "style_guide.md"
    if style_file.exists():
        content = style_file.read_text(encoding="utf-8")
        sections = content.split("## ")
        for idx, sec in enumerate(sections):
            sec = sec.strip()
            if not sec or sec.startswith("# "):
                continue
            lines = sec.split("\n", 1)
            header = lines[0].strip()
            body = lines[1].strip() if len(lines) > 1 else ""
            baseline_records.append(CanonicalRecord(
                id=f"style_guide_{idx}",
                text=f"Python Style Rule: {header}\n{body}",
                source="style_guide",
                language="python",
                type="convention",
                tags=["python", "style_guide"],
                metadata={"category": header}
            ))

    # Common Patterns
    pattern_file = RAW_DATA_DIR / "common_patterns.json"
    if pattern_file.exists():
        with open(pattern_file, "r", encoding="utf-8") as f:
            patterns = json.load(f)
        for item in patterns:
            text = (
                f"Code Pattern: {item.get('title')}\n"
                f"Description: {item.get('description')}\n"
                f"Bad Example:\n{item.get('bad_example')}\n"
                f"Good Example:\n{item.get('good_example')}"
            )
            baseline_records.append(CanonicalRecord(
                id=f"pattern_{item.get('id')}",
                text=text,
                source="common_patterns",
                language="python",
                type="pattern",
                tags=["python", "anti_pattern"],
                metadata={"category": item.get("category")}
            ))

    return baseline_records


def deduplicate_and_filter_records(
    raw_records: List[CanonicalRecord]
) -> Tuple[List[CanonicalRecord], Dict[str, Any]]:
    """
    Data Quality Pipeline:
    - Normalizes text & validates schema
    - Removes empty or malformed records
    - Deduplicates identical content using MD5 content hash
    - Tracks complete ingestion & filter metrics
    """
    seen_hashes = set()
    accepted: List[CanonicalRecord] = []

    stats = {
        "records_loaded": len(raw_records),
        "records_accepted": 0,
        "records_rejected": 0,
        "records_deduplicated": 0,
        "by_source": {},
        "by_language": {},
        "by_type": {}
    }

    for rec in raw_records:
        rec.text = sanitize_text(rec.text)

        # Validation check
        if not validate_canonical_record(rec):
            stats["records_rejected"] += 1
            continue

        # Content hash for deduplication
        content_hash = hashlib.md5(rec.text.encode("utf-8")).hexdigest()
        if content_hash in seen_hashes:
            stats["records_deduplicated"] += 1
            continue

        seen_hashes.add(content_hash)
        accepted.append(rec)

        # Update breakdown stats
        stats["by_source"][rec.source] = stats["by_source"].get(rec.source, 0) + 1
        stats["by_language"][rec.language] = stats["by_language"].get(rec.language, 0) + 1
        stats["by_type"][rec.type] = stats["by_type"].get(rec.type, 0) + 1

    stats["records_accepted"] = len(accepted)
    return accepted, stats


def log_statistics(stats: Dict[str, Any]) -> None:
    """Print readable preprocessing & quality statistics log."""
    print("\n" + "=" * 60)
    print("        RAG DATA INGESTION & QUALITY STATISTICS")
    print("=" * 60)
    print(f" Total Records Loaded:       {stats['records_loaded']}")
    print(f" Records Accepted:           {stats['records_accepted']}")
    print(f" Records Rejected (Malformed): {stats['records_rejected']}")
    print(f" Records Deduplicated:       {stats['records_deduplicated']}")
    print("-" * 60)
    print(" Breakdown by Data Source:")
    for src, count in stats['by_source'].items():
        print(f"   - {src}: {count}")
    print("-" * 60)
    print(" Breakdown by Programming Language:")
    for lang, count in stats['by_language'].items():
        print(f"   - {lang}: {count}")
    print("-" * 60)
    print(" Breakdown by Review/Document Type:")
    for rtype, count in stats['by_type'].items():
        print(f"   - {rtype}: {count}")
    print("=" * 60 + "\n")


def ingest_to_chromadb(records: List[CanonicalRecord]) -> None:
    """Ingest canonical records idempotently into ChromaDB vector store."""
    if not records:
        logger.warning("No records available to ingest into ChromaDB.")
        return

    logger.info(f"Connecting to ChromaDB at: {CHROMA_PERSIST_DIR}")
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

    # Perform idempotent batch upserts
    batch_size = 500
    for i in range(0, len(records), batch_size):
        end_idx = i + batch_size
        collection.upsert(
            ids=ids[i:end_idx],
            documents=documents[i:end_idx],
            metadatas=metadatas[i:end_idx]
        )

    logger.info(f"Successfully upserted {len(records)} records into ChromaDB collection '{COLLECTION_NAME}'.")


def run_ingestion(
    max_github_records: int = 1000,
    max_ms_per_category: int = 500
) -> Dict[str, Any]:
    """Run full RAG ingestion pipeline across all dataset adapters."""
    logger.info("Starting Multi-Dataset RAG Ingestion Pipeline...")

    # Load from adapters
    github_records = load_github_codereview(split="train", max_records=max_github_records)
    ms_records = load_microsoft_codereviewer(max_per_category=max_ms_per_category)
    baseline_records = load_raw_baseline_chunks()

    all_raw_records = github_records + ms_records + baseline_records

    # Data Quality Preprocessing & Statistics
    accepted_records, stats = deduplicate_and_filter_records(all_raw_records)
    log_statistics(stats)

    # Save intermediate processed canonical chunks
    processed_json_path = PROCESSED_DATA_DIR / "canonical_chunks.json"
    with open(processed_json_path, "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in accepted_records], f, indent=2)
    logger.info(f"Saved canonical records to: {processed_json_path}")

    # Upsert to ChromaDB
    ingest_to_chromadb(accepted_records)
    logger.info("RAG Ingestion Pipeline finished successfully!")

    return stats


if __name__ == "__main__":
    run_ingestion()
