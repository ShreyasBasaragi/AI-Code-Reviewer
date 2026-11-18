import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from evaluation.chroma_snapshot import (
    SnapshotVerificationError,
    compare_working_copy_to_snapshot,
    directory_sha256,
    load_snapshot_manifest,
    validate_saved_snapshot,
    write_initial_snapshot_manifest,
)


COLLECTION = "critique_code_context_v2"


def _database(path: Path, *, collection_id: str = "col-1", count: int = 3) -> None:
    path.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path / "chroma.sqlite3")
    connection.executescript(
        """
        CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT, dimension INTEGER);
        CREATE TABLE collection_metadata (
            collection_id TEXT, key TEXT, str_value TEXT, int_value INTEGER,
            float_value REAL, bool_value INTEGER
        );
        CREATE TABLE segments (id TEXT PRIMARY KEY, type TEXT, scope TEXT, collection TEXT);
        CREATE TABLE embeddings (id INTEGER PRIMARY KEY, segment_id TEXT, embedding_id TEXT);
        CREATE TABLE embedding_metadata (
            id INTEGER, key TEXT, string_value TEXT, int_value INTEGER,
            float_value REAL, bool_value INTEGER
        );
        """
    )
    connection.execute("INSERT INTO collections VALUES (?, ?, ?)", (collection_id, COLLECTION, 384))
    connection.execute("INSERT INTO segments VALUES (?, ?, ?, ?)",
                       ("seg-1", "urn:chroma:segment/metadata/sqlite", "METADATA", collection_id))
    connection.execute("INSERT INTO segments VALUES (?, ?, ?, ?)",
                       ("vec-1", "urn:chroma:segment/vector/hnsw-local-persisted", "VECTOR", collection_id))
    connection.execute("INSERT INTO collection_metadata VALUES (?, ?, ?, ?, ?, ?)",
                       (collection_id, "hnsw:space", "cosine", None, None, None))
    for index in range(count):
        connection.execute("INSERT INTO embeddings VALUES (?, ?, ?)", (index + 1, "seg-1", f"id-{index}"))
        connection.execute("INSERT INTO embedding_metadata VALUES (?, ?, ?, ?, ?, ?)",
                           (index + 1, "document", f"doc-{index}", None, None, None))
    connection.commit()
    connection.close()
    (path / "vec-1").mkdir()
    (path / "vec-1" / "data_level0.bin").write_bytes(b"vector-index")
    (path / "index.bin").write_bytes(b"vector-index-v1")


@pytest.fixture
def snapshot_dirs(tmp_path):
    canonical = tmp_path / "canonical_v2"
    copy_root = tmp_path / "copies"
    snapshot = copy_root / "chroma_db_v2"
    _database(canonical)
    snapshot.parent.mkdir(parents=True)
    shutil.copytree(canonical, snapshot)
    return canonical, copy_root, snapshot


def _manifest(canonical, copy_root, snapshot):
    return write_initial_snapshot_manifest(
        canonical_path=canonical, copy_path=snapshot, copy_root=copy_root,
        version="v2", collection_name=COLLECTION,
        quiescence_evidence="test fixture represents guarded quiescent copy",
    )


def _checkpoint(snapshot, *, snapshot_hash=None):
    return {"config": {"rag_version": "v2",
                        "rag_snapshot_sha256": snapshot_hash or directory_sha256(snapshot)}}


def test_resume_uses_saved_snapshot_when_canonical_sqlite_changes(snapshot_dirs):
    canonical, copy_root, snapshot = snapshot_dirs
    manifest_path = _manifest(canonical, copy_root, snapshot)
    checkpoint = _checkpoint(snapshot)
    with (canonical / "chroma.sqlite3").open("ab") as stream:
        stream.write(b"canonical-changed-after-copy")

    result = validate_saved_snapshot(
        copy_root=copy_root, version="v2", copy_path=snapshot,
        collection_name=COLLECTION, checkpoint=checkpoint, expected_record_count=3,
    )

    assert result["status"] == "manifest_verified"
    assert result["manifest_path"] == str(manifest_path)
    assert result["provenance"] == "initial_quiescent_copy_verified_by_saved_manifest"


def test_modified_temporary_copy_fails_snapshot_integrity(snapshot_dirs):
    canonical, copy_root, snapshot = snapshot_dirs
    _manifest(canonical, copy_root, snapshot)
    (snapshot / "index.bin").write_bytes(b"modified")

    with pytest.raises(SnapshotVerificationError, match="digest differs"):
        validate_saved_snapshot(copy_root=copy_root, version="v2", copy_path=snapshot,
                                collection_name=COLLECTION, expected_record_count=3)


def test_initial_copy_requires_matching_directory_digest(snapshot_dirs):
    canonical, copy_root, snapshot = snapshot_dirs
    with (canonical / "chroma.sqlite3").open("ab") as stream:
        stream.write(b"source changed")

    with pytest.raises(SnapshotVerificationError, match="initial copy directory digest"):
        _manifest(canonical, copy_root, snapshot)
    assert not list((copy_root / "snapshot_manifests").glob("*.json"))


def test_missing_manifest_is_not_retroactively_created(snapshot_dirs):
    _, copy_root, snapshot = snapshot_dirs

    with pytest.raises(SnapshotVerificationError, match="manifest"):
        validate_saved_snapshot(copy_root=copy_root, version="v2", copy_path=snapshot,
                                collection_name=COLLECTION, expected_record_count=3,
                                checkpoint=_checkpoint(snapshot), allow_legacy_checkpoint=False)

    assert not list((copy_root / "snapshot_manifests").glob("*.json"))


def test_legacy_checkpoint_hash_match_reports_unproven_initial_provenance(snapshot_dirs):
    _, copy_root, snapshot = snapshot_dirs
    checkpoint = _checkpoint(snapshot)

    result = validate_saved_snapshot(copy_root=copy_root, version="v2", copy_path=snapshot,
                                      collection_name=COLLECTION, expected_record_count=3,
                                      checkpoint=checkpoint)

    assert result["status"] == "legacy_checkpoint_hash_match"
    assert result["safe_to_resume"] is True
    assert "provenance_not_established" in result["provenance"]
    assert result["manifest_path"] is None
    assert not list((copy_root / "snapshot_manifests").glob("*.json"))


def test_legacy_checkpoint_mismatch_fails_without_manifest(snapshot_dirs):
    _, copy_root, snapshot = snapshot_dirs
    checkpoint = _checkpoint(snapshot, snapshot_hash="f" * 64)

    with pytest.raises(SnapshotVerificationError, match="pinned snapshot hash"):
        validate_saved_snapshot(copy_root=copy_root, version="v2", copy_path=snapshot,
                                collection_name=COLLECTION, expected_record_count=3,
                                checkpoint=checkpoint)
    assert not list((copy_root / "snapshot_manifests").glob("*.json"))


def test_mismatched_checkpoint_snapshot_hash_fails(snapshot_dirs):
    canonical, copy_root, snapshot = snapshot_dirs
    _manifest(canonical, copy_root, snapshot)
    checkpoint = _checkpoint(snapshot, snapshot_hash="0" * 64)

    with pytest.raises(SnapshotVerificationError, match="checkpoint snapshot digest"):
        validate_saved_snapshot(copy_root=copy_root, version="v2", copy_path=snapshot,
                                collection_name=COLLECTION, checkpoint=checkpoint,
                                expected_record_count=3)


def test_successful_manifest_bound_resume(snapshot_dirs):
    canonical, copy_root, snapshot = snapshot_dirs
    _manifest(canonical, copy_root, snapshot)
    checkpoint = _checkpoint(snapshot)

    result = validate_saved_snapshot(copy_root=copy_root, version="v2", copy_path=snapshot,
                                      collection_name=COLLECTION, checkpoint=checkpoint,
                                      expected_record_count=3)

    assert result["safe_to_resume"] is True
    assert result["snapshot_directory_sha256"] == checkpoint["config"]["rag_snapshot_sha256"]
    assert result["collection"]["record_count"] == 3
    _, manifest = load_snapshot_manifest(copy_root, "v2")
    assert manifest["snapshot_directory_sha256"] == result["snapshot_directory_sha256"]


def test_manifest_tampering_is_detected(snapshot_dirs):
    canonical, copy_root, snapshot = snapshot_dirs
    path = _manifest(canonical, copy_root, snapshot)
    path.chmod(0o644)
    value = json.loads(path.read_text())
    value["collection"]["record_count"] = 999
    path.write_text(json.dumps(value))

    with pytest.raises(SnapshotVerificationError, match="writable|integrity"):
        load_snapshot_manifest(copy_root, "v2")


def test_working_copy_diagnostics_distinguish_sqlite_index_and_record_changes():
    baseline = {"collection_id": "id", "collection_name": COLLECTION,
                "record_count": 3, "dimension": 384, "distance_metric": "cosine",
                "corpus_version": "2", "collection_content_sha256": "records-a",
                "vector_index_sha256": "index-a", "sqlite_sha256": "sqlite-a",
                "sqlite_journal_sha256": "journal-a"}
    sqlite_only = {**baseline, "sqlite_sha256": "sqlite-b"}
    assert compare_working_copy_to_snapshot(baseline, sqlite_only)["classification"] == \
        "sqlite_operational_or_metadata_bytes_changed"
    record_change = {**baseline, "collection_content_sha256": "records-b"}
    assert compare_working_copy_to_snapshot(baseline, record_change)["classification"] == \
        "collection_records_or_embeddings_changed"
    index_change = {**baseline, "vector_index_sha256": "index-b"}
    assert compare_working_copy_to_snapshot(baseline, index_change)["classification"] == \
        "vector_index_files_changed"


def test_invalid_manifest_does_not_downgrade_to_legacy_hash(snapshot_dirs):
    canonical, copy_root, snapshot = snapshot_dirs
    path = _manifest(canonical, copy_root, snapshot)
    path.chmod(0o644)

    with pytest.raises(SnapshotVerificationError, match="writable"):
        validate_saved_snapshot(copy_root=copy_root, version="v2", copy_path=snapshot,
                                collection_name=COLLECTION, checkpoint=_checkpoint(snapshot),
                                expected_record_count=3)
