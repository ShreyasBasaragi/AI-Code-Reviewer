"""Validated, isolated construction of the v3 ChromaDB collection."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable

from rag.config import PROJECT_ROOT
from rag.prepare_v3 import ARTIFACT_NAMES
from rag.v3 import INGESTION_VERSION
from rag.v3.prepare import _body_field
from rag.v3.schema import V3EmbeddingChunk, V3ParentRecord, content_hash

COLLECTION_NAME = "critique_code_context_v3"
MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
EXPECTED_DIMENSION = 384
DISTANCE_METRIC = "cosine"
NORMALIZE_EMBEDDINGS = False
DEFAULT_BATCH_SIZE = 128
REQUIRED_CHUNK_FIELDS = (
    "corpus_version", "ingestion_version", "source_family", "source_category",
    "source_document_id", "record_id", "record_type", "chunk_index", "chunk_count",
    "content_hash", "language", "language_origin", "language_confidence", "source_revision",
    "role", "source_field",
)


class BuildError(RuntimeError):
    pass


@dataclass
class PreparedArtifactSummary:
    directory: Path
    manifest: dict[str, Any]
    parents: dict[str, V3ParentRecord]
    chunk_ids: set[str]
    chunks_by_source: Counter
    chunks_by_category: Counter
    chunks_by_language: Counter
    chunks_by_type: Counter
    chunks_by_parent: dict[str, list[tuple[int, int]]]
    chunk_count: int
    parent_counts: Counter


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _read_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                raise BuildError(f"blank JSONL row at {path.name}:{line_number}")
            try:
                yield line_number, json.loads(line)
            except json.JSONDecodeError as exc:
                raise BuildError(f"invalid JSON at {path.name}:{line_number}: {exc.msg}") from exc


def _source_value(parent: V3ParentRecord, field_name: str) -> str:
    if field_name == "description_and_mitigation":
        return _body_field(parent)[1]
    value = parent.fields.get(field_name)
    if not isinstance(value, str):
        raise BuildError(f"parent {parent.record_id} lacks string source field {field_name!r}")
    return value


def _validate_source_spans(
    parents: dict[str, V3ParentRecord],
    chunks_file: Path,
    rows_by_source_field: dict[tuple[str, str], list[tuple[int, int, str]]],
) -> None:
    for (record_id, field_name), spans in rows_by_source_field.items():
        source = _source_value(parents[record_id], field_name)
        ordered = sorted(spans)
        cursor = 0
        for start, end, span_text in ordered:
            if start != cursor or end < start or source[start:end] != span_text:
                raise BuildError(f"non-lossless source spans for {record_id}:{field_name}")
            cursor = end
        if cursor != len(source):
            raise BuildError(f"incomplete source span coverage for {record_id}:{field_name}")


def validate_prepared_artifacts(directory: Path) -> PreparedArtifactSummary:
    """Validate hashes, schema, IDs, parent relations, spans, and manifest counts."""
    directory = directory.resolve(strict=True)
    manifest_path = directory / ARTIFACT_NAMES["manifest"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read preparation manifest: {type(exc).__name__}") from exc
    if manifest.get("corpus_version") != "3":
        raise BuildError("preparation manifest corpus_version must be 3")
    expected_artifacts = {key: ARTIFACT_NAMES[key] for key in ARTIFACT_NAMES if key != "manifest"}
    if manifest.get("artifacts") != expected_artifacts:
        raise BuildError("preparation artifact filenames do not match the v3 contract")
    parents_path = directory / ARTIFACT_NAMES["parents"]
    chunks_path = directory / ARTIFACT_NAMES["chunks"]
    for path in (parents_path, chunks_path,
                 directory / ARTIFACT_NAMES["rejections"],
                 directory / ARTIFACT_NAMES["duplicates"]):
        if not path.is_file():
            raise BuildError(f"missing preparation artifact: {path.name}")

    parents: dict[str, V3ParentRecord] = {}
    parent_counts: Counter = Counter()
    for line_number, raw in _read_jsonl(parents_path):
        try:
            parent = V3ParentRecord(**raw)
            parent.validate()
        except (TypeError, ValueError) as exc:
            raise BuildError(f"invalid parent at line {line_number}: {exc}") from exc
        if parent.record_id in parents:
            raise BuildError(f"duplicate parent record ID: {parent.record_id}")
        if content_hash(parent.fields) != parent.content_hash:
            raise BuildError(f"parent content hash mismatch: {parent.record_id}")
        parents[parent.record_id] = parent
        parent_counts[parent.source_family] += 1

    chunk_ids: set[str] = set()
    chunks_by_source: Counter = Counter()
    chunks_by_category: Counter = Counter()
    chunks_by_language: Counter = Counter()
    chunks_by_type: Counter = Counter()
    chunks_by_parent: dict[str, list[tuple[int, int]]] = defaultdict(list)
    spans: dict[tuple[str, str], list[tuple[int, int, str]]] = defaultdict(list)
    related_ids: list[tuple[str, ...]] = []
    chunk_count = 0
    observed_token_max = 0
    for line_number, raw in _read_jsonl(chunks_path):
        if not isinstance(raw, dict):
            raise BuildError(f"chunk row is not an object at line {line_number}")
        try:
            chunk_raw = dict(raw)
            chunk_raw["related_chunk_ids"] = tuple(chunk_raw.get("related_chunk_ids", ()))
            chunk = V3EmbeddingChunk(**chunk_raw)
            chunk.validate()
            metadata = chunk.metadata()
        except (TypeError, ValueError) as exc:
            raise BuildError(f"invalid chunk at line {line_number}: {exc}") from exc
        if chunk.chunk_id in chunk_ids:
            raise BuildError(f"duplicate chunk ID: {chunk.chunk_id}")
        if chunk.record_id not in parents:
            raise BuildError(f"chunk references missing parent: {chunk.record_id}")
        parent = parents[chunk.record_id]
        if (chunk.source_document_id != parent.source_document_id
                or chunk.source_family != parent.source_family
                or chunk.source_revision != parent.source_revision
                or chunk.language != parent.language
                or chunk.language_origin != parent.language_origin):
            raise BuildError(f"chunk/parent provenance mismatch: {chunk.chunk_id}")
        if not chunk.embedding_text.strip():
            raise BuildError(f"empty embedding text: {chunk.chunk_id}")
        observed_token_max = max(observed_token_max, chunk.token_count)
        if content_hash(chunk.embedding_text) != chunk.content_hash:
            raise BuildError(f"chunk content hash mismatch: {chunk.chunk_id}")
        if any(not isinstance(value, (str, int, float, bool)) for value in metadata.values()):
            raise BuildError(f"non-scalar Chroma metadata: {chunk.chunk_id}")
        if not set(REQUIRED_CHUNK_FIELDS).issubset(metadata):
            raise BuildError(f"missing required Chroma metadata: {chunk.chunk_id}")
        if chunk.source_span_start is None or chunk.source_span_end is None:
            raise BuildError(f"missing source offsets: {chunk.chunk_id}")
        spans[(chunk.record_id, chunk.source_field)].append(
            (chunk.source_span_start, chunk.source_span_end, chunk.source_span)
        )
        chunk_ids.add(chunk.chunk_id)
        related_ids.append(chunk.related_chunk_ids)
        chunks_by_parent[chunk.record_id].append((chunk.chunk_index, chunk.chunk_count))
        chunks_by_source[chunk.source_family] += 1
        chunks_by_category[chunk.source_category] += 1
        chunks_by_language[chunk.language] += 1
        chunks_by_type[chunk.record_type] += 1
        chunk_count += 1

    for parent_id, records in chunks_by_parent.items():
        expected_count = records[0][1]
        indices = [index for index, count in records]
        if any(count != expected_count for _index, count in records):
            raise BuildError(f"inconsistent chunk_count for parent {parent_id}")
        if len(indices) != expected_count or sorted(indices) != list(range(expected_count)):
            raise BuildError(f"inconsistent chunk indices/count for parent {parent_id}")
    missing_chunk_parents = set(parents) - set(chunks_by_parent)
    if missing_chunk_parents:
        raise BuildError(f"parents have no embedding chunks: {len(missing_chunk_parents)}")
    if any(set(row) - chunk_ids for row in related_ids):
        raise BuildError("chunk has unknown related_chunk_ids")
    _validate_source_spans(parents, chunks_path, spans)

    expected_parent_counts = manifest.get("parent_counts_by_source", {})
    expected_chunk_counts = manifest.get("chunk_counts_by_source", {})
    if dict(sorted(parent_counts.items())) != expected_parent_counts:
        raise BuildError("parent source counts disagree with preparation manifest")
    if dict(sorted(chunks_by_source.items())) != expected_chunk_counts:
        raise BuildError("chunk source counts disagree with preparation manifest")
    if chunk_count != sum(expected_chunk_counts.values()):
        raise BuildError("chunk count disagrees with preparation manifest")
    if observed_token_max != manifest.get("embedding_token_length_statistics", {}).get("max"):
        raise BuildError("observed max chunk token count disagrees with preparation manifest")
    if observed_token_max > manifest.get("max_chunk_tokens", 0):
        raise BuildError("prepared chunk exceeds its configured token budget")
    duplicate_count = 0
    for line_number, row in _read_jsonl(directory / ARTIFACT_NAMES["duplicates"]):
        if (not isinstance(row, dict) or row.get("record_id_a") not in parents
                or row.get("record_id_b") not in parents or not row.get("kind")):
            raise BuildError(f"invalid duplicate-candidate record at line {line_number}")
        duplicate_count += 1
    if duplicate_count != manifest.get("exact_or_near_duplicate_candidate_count"):
        raise BuildError("duplicate-candidate count disagrees with preparation manifest")
    rejection_count = 0
    for line_number, row in _read_jsonl(directory / ARTIFACT_NAMES["rejections"]):
        if not isinstance(row, dict) or not row.get("reason"):
            raise BuildError(f"invalid rejection record at line {line_number}")
        rejection_count += 1
    if rejection_count != manifest.get("rejected_record_count"):
        raise BuildError("rejection count disagrees with preparation manifest")

    return PreparedArtifactSummary(
        directory=directory, manifest=manifest, parents=parents, chunk_ids=chunk_ids,
        chunks_by_source=chunks_by_source, chunks_by_category=chunks_by_category,
        chunks_by_language=chunks_by_language, chunks_by_type=chunks_by_type,
        chunks_by_parent=chunks_by_parent, chunk_count=chunk_count, parent_counts=parent_counts,
    )


def validate_build_paths(project_root: Path) -> tuple[Path, Path, Path]:
    """Resolve fixed v3 paths and reject aliases, symlinks, v2, or arbitrary locations."""
    root = project_root.resolve(strict=True)
    data_dir = root / "rag" / "data"
    if data_dir.is_symlink():
        raise BuildError("rag/data must not be a symlink for v3 build paths")
    resolved_data = data_dir.resolve(strict=True)
    if resolved_data != data_dir:
        raise BuildError("rag/data resolves outside its expected project path")
    staging = data_dir / "chroma_db_v3.staging"
    final = data_dir / "chroma_db_v3"
    v2 = (data_dir / "chroma_db_v2").resolve(strict=True)
    for target in (staging, final):
        if target.is_symlink() or target.exists():
            raise BuildError(f"refusing existing or symlink build path: {target.name}")
        if target.resolve(strict=False).parent != resolved_data:
            raise BuildError(f"unsafe v3 build path: {target}")
        if target.resolve(strict=False) == v2:
            raise BuildError("v3 build path resolves to the v2 database")
    return resolved_data, staging, final


def _load_local_embedding_model(model_id: str = MODEL_ID):
    try:
        from huggingface_hub import snapshot_download
        from sentence_transformers import SentenceTransformer
        snapshot = Path(snapshot_download(model_id, local_files_only=True)).resolve(strict=True)
    except Exception as exc:
        raise BuildError(
            f"embedding model {model_id!r} is not fully cached locally; no download was attempted"
        ) from exc
    try:
        model = SentenceTransformer(str(snapshot), device="cpu", local_files_only=True)
    except Exception as exc:
        raise BuildError(f"cannot load cached embedding model: {type(exc).__name__}") from exc
    return model, snapshot.name


def _encode(embedder, texts: list[str], *, batch_size: int, expected_dimension: int):
    import numpy as np

    vectors = np.asarray(embedder.encode(
        texts, batch_size=batch_size, convert_to_numpy=True,
        normalize_embeddings=NORMALIZE_EMBEDDINGS, show_progress_bar=False,
    ), dtype=np.float32)
    if vectors.shape != (len(texts), expected_dimension):
        raise BuildError(f"invalid embedding shape {vectors.shape}; expected ({len(texts)}, {expected_dimension})")
    if not np.isfinite(vectors).all():
        raise BuildError("embedding batch contains nonfinite values")
    norms = np.linalg.norm(vectors, axis=1)
    if np.any(norms <= 0):
        raise BuildError("embedding batch contains a zero-length vector")
    return vectors


def _assert_round_trip(collection, rows: list[dict[str, Any]]) -> dict[str, float]:
    import numpy as np

    ids = [row["chunk_id"] for row in rows]
    stored = collection.get(ids=ids, include=["documents", "metadatas", "embeddings"])
    if set(stored["ids"]) != set(ids):
        raise BuildError("inserted batch IDs did not round-trip")
    by_id = {item_id: index for index, item_id in enumerate(stored["ids"])}
    norms = []
    for row in rows:
        index = by_id[row["chunk_id"]]
        expected_meta = _chunk_metadata(row)
        if stored["documents"][index] != row["embedding_text"]:
            raise BuildError(f"stored document mismatch: {row['chunk_id']}")
        if stored["metadatas"][index] != expected_meta:
            raise BuildError(f"stored metadata mismatch: {row['chunk_id']}")
        vector = np.asarray(stored["embeddings"][index], dtype=np.float32)
        if vector.shape != (EXPECTED_DIMENSION,) or not np.isfinite(vector).all():
            raise BuildError(f"stored vector invalid: {row['chunk_id']}")
        norm = float(np.linalg.norm(vector))
        if not math.isfinite(norm) or norm <= 0:
            raise BuildError(f"stored vector norm invalid: {row['chunk_id']}")
        norms.append(norm)
    return {"min_norm": min(norms), "max_norm": max(norms)} if norms else {"min_norm": 0, "max_norm": 0}


def _chunk_metadata(row: dict[str, Any]) -> dict[str, Any]:
    raw = dict(row)
    raw["related_chunk_ids"] = tuple(raw.get("related_chunk_ids", ()))
    return V3EmbeddingChunk(**raw).metadata()


def _jsonl_rows(path: Path):
    for _line, row in _read_jsonl(path):
        yield row


def _collection_full_validation(collection, summary: PreparedArtifactSummary) -> dict[str, Any]:
    import numpy as np

    if collection.name != COLLECTION_NAME:
        raise BuildError(f"unexpected collection name: {collection.name}")
    metadata = collection.metadata or {}
    if metadata.get("hnsw:space") != DISTANCE_METRIC:
        raise BuildError("collection distance metric is not cosine")
    if collection.count() != summary.chunk_count:
        raise BuildError("collection count differs from prepared chunk count")
    seen: set[str] = set()
    source_counts: Counter = Counter()
    category_counts: Counter = Counter()
    language_counts: Counter = Counter()
    type_counts: Counter = Counter()
    dimension = None
    min_norm = float("inf")
    max_norm = 0.0
    offset = 0
    page_size = 256
    while offset < summary.chunk_count:
        page = collection.get(limit=page_size, offset=offset,
                              include=["documents", "metadatas", "embeddings"])
        ids = page.get("ids", [])
        if not ids:
            break
        if seen.intersection(ids):
            raise BuildError("duplicate ID returned by collection paging")
        seen.update(ids)
        for index, item_id in enumerate(ids):
            if item_id not in summary.chunk_ids:
                raise BuildError(f"unexpected collection ID: {item_id}")
            document = page["documents"][index]
            metadata_row = page["metadatas"][index]
            vector = np.asarray(page["embeddings"][index], dtype=np.float32)
            if not document or not document.strip():
                raise BuildError(f"empty stored document: {item_id}")
            if metadata_row.get("record_id") not in summary.parents:
                raise BuildError(f"stored chunk references unknown parent: {item_id}")
            vector_dimension = int(vector.shape[0]) if vector.ndim == 1 else -1
            dimension = vector_dimension if dimension is None else dimension
            if vector_dimension != EXPECTED_DIMENSION or vector_dimension != dimension:
                raise BuildError(f"stored embedding dimension invalid: {item_id}")
            if not np.isfinite(vector).all():
                raise BuildError(f"stored embedding contains nonfinite values: {item_id}")
            norm = float(np.linalg.norm(vector))
            if not math.isfinite(norm) or norm <= 0:
                raise BuildError(f"stored embedding is zero/invalid: {item_id}")
            min_norm, max_norm = min(min_norm, norm), max(max_norm, norm)
            source_counts[metadata_row["source_family"]] += 1
            category_counts[metadata_row["source_category"]] += 1
            language_counts[metadata_row["language"]] += 1
            type_counts[metadata_row["record_type"]] += 1
        offset += len(ids)
    if seen != summary.chunk_ids:
        raise BuildError(f"collection ID set mismatch: missing={len(summary.chunk_ids-seen)}")
    if dict(sorted(source_counts.items())) != dict(sorted(summary.chunks_by_source.items())):
        raise BuildError("stored source counts disagree with prepared artifacts")
    return {
        "collection_name": collection.name,
        "collection_metadata": metadata,
        "record_count": len(seen),
        "vector_count": len(seen),
        "embedding_dimension": dimension,
        "embedding_norm_min": min_norm,
        "embedding_norm_max": max_norm,
        "parent_count": len(summary.parents),
        "parent_counts_by_source_family": dict(sorted(summary.parent_counts.items())),
        "parent_chunk_distribution": dict(sorted(Counter(
            str(len(indices)) for indices in summary.chunks_by_parent.values()
        ).items(), key=lambda item: int(item[0]))),
        "counts_by_source_family": dict(sorted(source_counts.items())),
        "counts_by_source_category": dict(sorted(category_counts.items())),
        "counts_by_language": dict(sorted(language_counts.items())),
        "counts_by_record_type": dict(sorted(type_counts.items())),
        "unknown_language_parent_count": sum(1 for item in summary.parents.values()
                                              if item.language == "unknown"),
        "unknown_language_chunk_count": language_counts.get("unknown", 0),
        "source_specific_parent_counts": {
            family: summary.parent_counts.get(family, 0)
            for family in ("mitre_cwe", "owasp_cheatsheets", "bandit", "pep8", "google_style")
        },
        "source_specific_chunk_counts": {
            family: source_counts.get(family, 0)
            for family in ("mitre_cwe", "owasp_cheatsheets", "bandit", "pep8", "google_style")
        },
        "preparation_warnings": summary.manifest.get("warnings", []),
    }


def _retrieval_smoke_tests(collection, embedder, summary: PreparedArtifactSummary, top_k: int = 4):
    queries = {
        "python_subprocess_security": "Python subprocess shell=True with user controlled command input",
        "python_membership_off_by_one": "Python list membership and off-by-one loop boundary defect",
        "python_clean_code": "Python function with clear names and straightforward validation",
        "java_code": "Java method using a database query and input validation",
        "ruby_rails_code": "Ruby on Rails controller parameters and SQL injection prevention",
        "cwe_vulnerability": "Improper neutralization of input in a command or SQL query CWE",
    }
    output = {}
    for name, query in queries.items():
        vector = _encode(embedder, [query], batch_size=1, expected_dimension=EXPECTED_DIMENSION)
        result = collection.query(query_embeddings=vector.tolist(), n_results=top_k,
                                  include=["documents", "metadatas", "distances"])
        rows = []
        for index, chunk_id in enumerate(result["ids"][0]):
            meta = result["metadatas"][0][index]
            rows.append({
                "chunk_id": chunk_id,
                "source_family": meta.get("source_family"),
                "source_category": meta.get("source_category"),
                "language": meta.get("language"),
                "record_id": meta.get("record_id"),
                "chunk_index": meta.get("chunk_index"),
                "chunk_count": meta.get("chunk_count"),
                "distance": result["distances"][0][index],
                "document_characters": len(result["documents"][0][index] or ""),
                "parent_present": meta.get("record_id") in summary.parents,
            })
        output[name] = {"query": query, "results": rows}
    return output


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, sort_keys=True, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def build_v3(
    *,
    project_root: Path = PROJECT_ROOT,
    prepared_dir: Path | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    embedder=None,
    model_revision: str | None = None,
    client_factory: Callable[..., Any] | None = None,
    promote: bool = True,
) -> dict[str, Any]:
    """Build a new v3 staging DB; never overwrites a v2 or existing v3 path."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise BuildError("batch_size must be positive")
    project_root = project_root.resolve(strict=True)
    data_dir, staging, final = validate_build_paths(project_root)
    prepared_dir = (prepared_dir or project_root / "rag" / "data" / "processed" / "v3_preparation").resolve(strict=True)
    if not prepared_dir.is_dir():
        raise BuildError("prepared artifact directory is not a directory")
    summary = validate_prepared_artifacts(prepared_dir)
    if summary.chunk_count <= 0:
        raise BuildError("prepared corpus has no chunks; refusing to create a database")

    if embedder is None:
        embedder, detected_revision = _load_local_embedding_model()
        model_revision = model_revision or detected_revision
    else:
        model_revision = model_revision or "test-or-injected-model"
    model_dimension = getattr(embedder, "get_sentence_embedding_dimension", lambda: EXPECTED_DIMENSION)()
    if model_dimension != EXPECTED_DIMENSION:
        raise BuildError(f"embedding model dimension is {model_dimension}; expected {EXPECTED_DIMENSION}")
    max_seq_length = getattr(embedder, "max_seq_length", None)
    max_prepared_tokens = summary.manifest.get("embedding_token_length_statistics", {}).get("max")
    if (isinstance(max_seq_length, int) and isinstance(max_prepared_tokens, int)
            and max_prepared_tokens > max_seq_length):
        raise BuildError(
            f"prepared chunk maximum {max_prepared_tokens} exceeds model max_seq_length {max_seq_length}"
        )
    probe = _encode(embedder, ["v3 embedding configuration probe"], batch_size=1,
                    expected_dimension=EXPECTED_DIMENSION)
    probe_norm = float((probe[0] ** 2).sum() ** 0.5)
    if client_factory is None:
        import chromadb
        client_factory = chromadb.PersistentClient

    # Reserve staging only after all input and model checks. Existing paths are never removed.
    staging.mkdir(parents=False, exist_ok=False)
    manifest: dict[str, Any] = {
        "corpus_version": "3",
        "ingestion_version": summary.manifest.get("ingestion_version", INGESTION_VERSION),
        "preparation_manifest_sha256": sha256_path(prepared_dir / ARTIFACT_NAMES["manifest"]),
        "chunk_jsonl_sha256": sha256_path(prepared_dir / ARTIFACT_NAMES["chunks"]),
        "parent_count": len(summary.parents),
        "chunk_count": summary.chunk_count,
        "parent_counts_by_source": dict(sorted(summary.parent_counts.items())),
        "chunk_counts_by_source": dict(sorted(summary.chunks_by_source.items())),
        "embedding_model_id": MODEL_ID,
        "embedding_model_revision": model_revision,
        "embedding_dimension": EXPECTED_DIMENSION,
        "embedding_normalize_embeddings": NORMALIZE_EMBEDDINGS,
        "embedding_model_max_seq_length": max_seq_length,
        "embedding_configuration_probe_norm": probe_norm,
        "chroma_distance_metric": DISTANCE_METRIC,
        "chromadb_version": _version("chromadb"),
        "sentence_transformers_version": _version("sentence-transformers"),
        "transformers_version": _version("transformers"),
        "batch_size": batch_size,
        "collection_name": COLLECTION_NAME,
        "build_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "build_status": "started",
        "validation_status": "pending",
        "database_record_count": 0,
        "staging_directory": staging.relative_to(project_root).as_posix(),
        "final_directory": final.relative_to(project_root).as_posix(),
        "deterministic_inputs": {
            "preparation_manifest_sha256": sha256_path(prepared_dir / ARTIFACT_NAMES["manifest"]),
            "chunk_jsonl_sha256": sha256_path(prepared_dir / ARTIFACT_NAMES["chunks"]),
            "model_id": MODEL_ID,
            "model_revision": model_revision,
            "normalization": NORMALIZE_EMBEDDINGS,
            "distance_metric": DISTANCE_METRIC,
            "batch_size": batch_size,
        },
    }
    _atomic_json(staging / "build_manifest.json", manifest)
    client = None
    inserted = 0
    try:
        client = client_factory(path=str(staging))
        collection = client.create_collection(
            name=COLLECTION_NAME,
            embedding_function=None,
            metadata={
                "hnsw:space": DISTANCE_METRIC,
                "corpus_version": "3",
                "embedding_model_id": MODEL_ID,
                "embedding_dimension": EXPECTED_DIMENSION,
                "embedding_normalize_embeddings": NORMALIZE_EMBEDDINGS,
            },
        )
        chunk_iterator = iter(_jsonl_rows(prepared_dir / ARTIFACT_NAMES["chunks"]))
        while True:
            rows = []
            try:
                for _ in range(batch_size):
                    rows.append(next(chunk_iterator))
            except StopIteration:
                pass
            if not rows:
                break
            vectors = _encode(embedder, [row["embedding_text"] for row in rows],
                              batch_size=batch_size, expected_dimension=EXPECTED_DIMENSION)
            collection.add(
                ids=[row["chunk_id"] for row in rows],
                documents=[row["embedding_text"] for row in rows],
                metadatas=[_chunk_metadata(row) for row in rows],
                embeddings=vectors.tolist(),
            )
            _assert_round_trip(collection, rows)
            inserted += len(rows)
            manifest["database_record_count"] = inserted
            manifest["build_status"] = "inserting"
            _atomic_json(staging / "build_manifest.json", manifest)

        if inserted != summary.chunk_count:
            raise BuildError(f"inserted {inserted} records; expected {summary.chunk_count}")
        validation = _collection_full_validation(collection, summary)
        validation["retrieval_smoke_tests"] = _retrieval_smoke_tests(collection, embedder, summary)
        validation["source_count_reconciliation"] = {
            "expected": dict(sorted(summary.chunks_by_source.items())),
            "actual": validation["counts_by_source_family"],
            "matches": validation["counts_by_source_family"] == dict(sorted(summary.chunks_by_source.items())),
        }
        _atomic_json(staging / "validation_report.json", validation)
        manifest["build_status"] = "validated"
        manifest["validation_status"] = "passed"
        manifest["database_record_count"] = validation["record_count"]
        manifest["vector_count"] = validation["vector_count"]
        failure_directory = staging if staging.exists() else final if final.exists() else staging
        _atomic_json(failure_directory / "build_manifest.json", manifest)
        client.close()
        client = None

        if not promote:
            return {"build_manifest": manifest, "validation_report": validation,
                    "staging_directory": str(staging), "promoted": False}
        if final.exists() or final.is_symlink():
            raise BuildError("final v3 directory appeared before promotion; staging preserved")
        os.rename(staging, final)
        promoted_client = client_factory(path=str(final))
        try:
            promoted_collection = promoted_client.get_collection(COLLECTION_NAME, embedding_function=None)
            post = _collection_full_validation(promoted_collection, summary)
            if post["record_count"] != validation["record_count"]:
                raise BuildError("promoted database count differs from staging validation")
        except Exception:
            promoted_client.close()
            if not staging.exists() and final.exists():
                os.rename(final, staging)
            raise
        promoted_client.close()
        manifest["build_status"] = "promoted"
        manifest["validation_status"] = "passed"
        manifest["promotion_timestamp_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_json(final / "build_manifest.json", manifest)
        validation["post_promotion_validation"] = post
        _atomic_json(final / "validation_report.json", validation)
        processed_reports = project_root / "rag" / "data" / "processed"
        _atomic_json(processed_reports / "v3_build_manifest.json", manifest)
        _atomic_json(processed_reports / "v3_validation_report.json", validation)
        return {"build_manifest": manifest, "validation_report": validation,
                "final_directory": str(final), "promoted": True}
    except KeyboardInterrupt:
        manifest["build_status"] = "interrupted"
        manifest["validation_status"] = "not_completed"
        manifest["database_record_count"] = inserted
        failure_directory = staging if staging.exists() else final if final.exists() else staging
        _atomic_json(failure_directory / "build_manifest.json", manifest)
        raise
    except Exception as exc:
        manifest["build_status"] = "failed"
        manifest["validation_status"] = "failed"
        manifest["error_type"] = type(exc).__name__
        manifest["error_message"] = str(exc)[:1000]
        try:
            manifest["database_record_count"] = client.get_collection(COLLECTION_NAME).count() if client else inserted
        except Exception:
            manifest["database_record_count"] = inserted
        failure_directory = staging if staging.exists() else final if final.exists() else staging
        _atomic_json(failure_directory / "build_manifest.json", manifest)
        raise
    finally:
        if client is not None:
            client.close()
