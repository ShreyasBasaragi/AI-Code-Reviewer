import json
from pathlib import Path

import numpy as np
import pytest

from rag.prepare_v3 import ARTIFACT_NAMES, write_artifacts
from rag.v3.build import (
    BuildError,
    _encode,
    build_v3,
    sha256_path,
    validate_build_paths,
    validate_prepared_artifacts,
)
from rag.v3.prepare import PreparationConfig, prepare_local_sources

from test_rag_v3_preparation import CharTokenizer, _write_fixture_sources


class FakeEmbedder:
    max_seq_length = 256

    def get_sentence_embedding_dimension(self):
        return 384

    def encode(self, texts, **_kwargs):
        return np.ones((len(texts), 384), dtype=np.float32)


class WrongDimensionEmbedder(FakeEmbedder):
    def encode(self, texts, **_kwargs):
        return np.ones((len(texts), 12), dtype=np.float32)


class NonFiniteEmbedder(FakeEmbedder):
    def encode(self, texts, **_kwargs):
        result = np.ones((len(texts), 384), dtype=np.float32)
        result[0, 0] = np.nan
        return result


class ZeroEmbedder(FakeEmbedder):
    def encode(self, texts, **_kwargs):
        return np.zeros((len(texts), 384), dtype=np.float32)


@pytest.fixture
def prepared_artifacts(tmp_path):
    sources = tmp_path / "sources"
    _write_fixture_sources(sources)
    corpus = prepare_local_sources(
        input_root=sources, tokenizer=CharTokenizer(),
        config=PreparationConfig(max_chunk_tokens=200, tokenizer_name="fixture-char-tokenizer"),
    )
    prepared = tmp_path / "prepared"
    write_artifacts(corpus, prepared)
    return prepared


def _read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_rows(path, rows):
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _project_root(tmp_path):
    root = tmp_path / "project"
    (root / "rag" / "data" / "chroma_db_v2").mkdir(parents=True)
    (root / "rag" / "data" / "processed").mkdir(parents=True)
    return root


def test_prepared_artifact_loading_hashes_relations_and_manifest(prepared_artifacts):
    summary = validate_prepared_artifacts(prepared_artifacts)
    assert len(summary.parents) == 8
    assert summary.chunk_count > 0
    assert sum(summary.chunks_by_source.values()) == summary.chunk_count
    assert summary.manifest["rejected_record_count"] == 3
    assert summary.manifest["ingestion_version"]
    build_manifest_hash = sha256_path(prepared_artifacts / ARTIFACT_NAMES["manifest"])
    assert len(build_manifest_hash) == 64


def test_duplicate_chunk_ids_are_rejected(prepared_artifacts):
    path = prepared_artifacts / ARTIFACT_NAMES["chunks"]
    rows = _read_rows(path)
    rows.append(dict(rows[0]))
    _write_rows(path, rows)
    with pytest.raises(BuildError, match="duplicate chunk ID"):
        validate_prepared_artifacts(prepared_artifacts)


def test_missing_parent_is_rejected(prepared_artifacts):
    path = prepared_artifacts / ARTIFACT_NAMES["chunks"]
    rows = _read_rows(path)
    rows[0]["record_id"] = "missing-parent"
    _write_rows(path, rows)
    with pytest.raises(BuildError, match="missing parent"):
        validate_prepared_artifacts(prepared_artifacts)


def test_invalid_metadata_and_content_hash_are_rejected(prepared_artifacts):
    path = prepared_artifacts / ARTIFACT_NAMES["chunks"]
    original = path.read_text(encoding="utf-8")
    rows = _read_rows(path)
    rows[0]["language_confidence"] = "high"
    _write_rows(path, rows)
    with pytest.raises(BuildError, match="invalid chunk"):
        validate_prepared_artifacts(prepared_artifacts)
    rows[0]["language_confidence"] = 0.7
    rows[0]["content_hash"] = "wrong"
    _write_rows(path, rows)
    with pytest.raises(BuildError, match="content hash mismatch"):
        validate_prepared_artifacts(prepared_artifacts)
    path.write_text(original, encoding="utf-8")


@pytest.mark.parametrize(
    "embedder, message",
    [
        (WrongDimensionEmbedder(), "invalid embedding shape"),
        (NonFiniteEmbedder(), "nonfinite"),
        (ZeroEmbedder(), "zero-length"),
    ],
)
def test_embedding_batches_require_dimension_finite_nonzero(embedder, message):
    with pytest.raises(BuildError, match=message):
        _encode(embedder, ["text"], batch_size=1, expected_dimension=384)


def test_chroma_insert_roundtrip_smoke_and_source_count_reconciliation(prepared_artifacts, tmp_path):
    chromadb = pytest.importorskip("chromadb")
    root = _project_root(tmp_path)
    result = build_v3(
        project_root=root, prepared_dir=prepared_artifacts, batch_size=4,
        embedder=FakeEmbedder(), model_revision="fixture-revision",
        client_factory=lambda path: chromadb.PersistentClient(path=path),
    )
    assert result["promoted"] is True
    assert result["validation_report"]["record_count"] == result["build_manifest"]["chunk_count"]
    assert result["validation_report"]["source_count_reconciliation"]["matches"] is True
    assert len(result["validation_report"]["retrieval_smoke_tests"]) == 6
    assert (root / "rag" / "data" / "chroma_db_v3" / "build_manifest.json").is_file()
    assert result["build_manifest"]["preparation_manifest_sha256"] == sha256_path(
        prepared_artifacts / ARTIFACT_NAMES["manifest"]
    )


def test_staging_collision_and_symlink_are_refused(tmp_path):
    root = _project_root(tmp_path)
    staging = root / "rag" / "data" / "chroma_db_v3.staging"
    staging.mkdir()
    with pytest.raises(BuildError, match="existing or symlink"):
        validate_build_paths(root)
    staging.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    staging.symlink_to(outside, target_is_directory=True)
    with pytest.raises(BuildError, match="existing or symlink"):
        validate_build_paths(root)


def test_final_collision_is_refused_without_overwrite(prepared_artifacts, tmp_path):
    root = _project_root(tmp_path)
    final = root / "rag" / "data" / "chroma_db_v3"
    final.mkdir()
    with pytest.raises(BuildError, match="existing or symlink"):
        build_v3(project_root=root, prepared_dir=prepared_artifacts,
                 embedder=FakeEmbedder())
    assert final.is_dir() and not list(final.iterdir())


def test_preflight_failure_blocks_promotion(prepared_artifacts, tmp_path):
    root = _project_root(tmp_path)
    path = prepared_artifacts / ARTIFACT_NAMES["chunks"]
    rows = _read_rows(path)
    rows[0]["record_id"] = "missing"
    _write_rows(path, rows)
    with pytest.raises(BuildError, match="missing parent"):
        build_v3(project_root=root, prepared_dir=prepared_artifacts, embedder=FakeEmbedder())
    assert not (root / "rag" / "data" / "chroma_db_v3.staging").exists()
    assert not (root / "rag" / "data" / "chroma_db_v3").exists()


def test_interrupted_build_keeps_staging_and_manifest(prepared_artifacts, tmp_path):
    root = _project_root(tmp_path)

    class InterruptCollection:
        name = "critique_code_context_v3"
        metadata = {"hnsw:space": "cosine"}

        def add(self, **_kwargs):
            raise KeyboardInterrupt()

    class InterruptClient:
        def __init__(self, **_kwargs):
            self.collection = InterruptCollection()

        def create_collection(self, **_kwargs):
            return self.collection

        def get_collection(self, *_args, **_kwargs):
            return self.collection

        def close(self):
            pass

    with pytest.raises(KeyboardInterrupt):
        build_v3(project_root=root, prepared_dir=prepared_artifacts,
                 embedder=FakeEmbedder(), client_factory=InterruptClient)
    stage = root / "rag" / "data" / "chroma_db_v3.staging"
    manifest = json.loads((stage / "build_manifest.json").read_text())
    assert manifest["build_status"] == "interrupted"
    assert manifest["database_record_count"] == 0
    with pytest.raises(BuildError, match="existing or symlink"):
        validate_build_paths(root)


def test_v2_collection_contract_is_not_changed():
    from rag.config import CHROMA_PERSIST_DIR, COLLECTION_NAME
    assert CHROMA_PERSIST_DIR.name == "chroma_db_v2"
    assert COLLECTION_NAME == "critique_code_context_v2"
