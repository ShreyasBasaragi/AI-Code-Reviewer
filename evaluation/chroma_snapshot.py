"""Read-only Chroma snapshot inspection and immutable copy manifests."""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

MANIFEST_VERSION = 1
MANIFEST_DIRNAME = "snapshot_manifests"


class SnapshotVerificationError(ValueError):
    pass


def directory_sha256(path: Path) -> str:
    if not path.is_dir():
        raise SnapshotVerificationError(f"database directory does not exist: {path}")
    digest = hashlib.sha256()
    for item in sorted(value for value in path.rglob("*") if value.is_file()):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        with item.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_collection_readonly(path: Path, collection_name: str) -> dict[str, Any]:
    """Read collection identity and count directly from SQLite in immutable RO mode."""
    database = path / "chroma.sqlite3"
    if not database.is_file():
        raise SnapshotVerificationError(f"missing Chroma SQLite database: {database}")
    uri = database.resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            row = connection.execute(
                "SELECT id, name, dimension FROM collections WHERE name = ?", (collection_name,)
            ).fetchone()
            if row is None:
                raise SnapshotVerificationError(f"collection not found: {collection_name}")
            collection_id, name, dimension = row
            count = connection.execute(
                "SELECT COUNT(DISTINCT e.embedding_id) "
                "FROM embeddings e JOIN segments s ON s.id = e.segment_id "
                "WHERE s.collection = ? AND s.scope = 'METADATA'", (collection_id,)
            ).fetchone()[0]
            metadata = {
                key: value for key, value in connection.execute(
                    "SELECT key, COALESCE(str_value, CAST(int_value AS TEXT), "
                    "CAST(float_value AS TEXT), CAST(bool_value AS TEXT)) "
                    "FROM collection_metadata WHERE collection_id = ?", (collection_id,)
                )
            }
            content_digest = hashlib.sha256()
            content_rows = connection.execute(
                "SELECT e.embedding_id, m.key, m.string_value, m.int_value, m.float_value, m.bool_value "
                "FROM embeddings e JOIN segments s ON s.id = e.segment_id "
                "JOIN embedding_metadata m ON m.id = e.id "
                "WHERE s.collection = ? AND s.scope = 'METADATA' "
                "ORDER BY e.embedding_id, m.key", (collection_id,)
            )
            for content_row in content_rows:
                content_digest.update(json.dumps(content_row, ensure_ascii=False,
                                                 separators=(",", ":")).encode("utf-8"))
                content_digest.update(b"\n")
            vector_segments = [row[0] for row in connection.execute(
                "SELECT id FROM segments WHERE collection = ? AND scope = 'VECTOR' ORDER BY id",
                (collection_id,)
            )]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise SnapshotVerificationError(f"read-only Chroma metadata inspection failed: {exc}") from exc
    index_digest = hashlib.sha256()
    for segment_id in vector_segments:
        segment_path = path / segment_id
        if not segment_path.is_dir():
            raise SnapshotVerificationError(f"missing vector index segment directory: {segment_id}")
        for index_file in sorted(item for item in segment_path.rglob("*") if item.is_file()):
            index_digest.update(index_file.relative_to(path).as_posix().encode("utf-8"))
            with index_file.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    index_digest.update(block)
    journal_digest = hashlib.sha256()
    journal_files = []
    for journal in sorted(item for item in path.iterdir()
                          if item.is_file() and item.name.startswith("chroma.sqlite3-")
                          and item.name.endswith(("-wal", "-shm", "-journal"))):
        journal_files.append({"name": journal.name, "size_bytes": journal.stat().st_size,
                              "sha256": file_sha256(journal)})
        journal_digest.update(journal.name.encode("utf-8"))
        journal_digest.update(bytes.fromhex(journal_files[-1]["sha256"]))
    return {"collection_id": collection_id, "collection_name": name,
            "record_count": int(count), "dimension": dimension,
            "distance_metric": metadata.get("hnsw:space"),
            "corpus_version": metadata.get("corpus_version"),
            "collection_content_sha256": content_digest.hexdigest(),
            "vector_index_sha256": index_digest.hexdigest(),
            "sqlite_sha256": file_sha256(database),
            "sqlite_journal_files": journal_files,
            "sqlite_journal_sha256": journal_digest.hexdigest()}


def compare_working_copy_to_snapshot(snapshot: dict[str, Any], working: dict[str, Any]) -> dict[str, Any]:
    """Separate record/index changes from SQLite operational-byte changes."""
    semantic_keys = ("collection_id", "collection_name", "record_count", "dimension",
                     "distance_metric", "corpus_version", "collection_content_sha256")
    index_unchanged = snapshot.get("vector_index_sha256") == working.get("vector_index_sha256")
    semantic_unchanged = all(snapshot.get(key) == working.get(key) for key in semantic_keys)
    sqlite_unchanged = snapshot.get("sqlite_sha256") == working.get("sqlite_sha256")
    journals_unchanged = snapshot.get("sqlite_journal_sha256") == working.get("sqlite_journal_sha256")
    return {
        "semantic_collection_unchanged": semantic_unchanged,
        "vector_index_unchanged": index_unchanged,
        "sqlite_bytes_unchanged": sqlite_unchanged,
        "sqlite_journal_files_unchanged": journals_unchanged,
        "classification": ("byte_identical" if semantic_unchanged and index_unchanged and sqlite_unchanged
                           else "sqlite_operational_or_metadata_bytes_changed" if semantic_unchanged and index_unchanged
                           else "collection_records_or_embeddings_changed" if not semantic_unchanged
                           else "vector_index_files_changed"),
    }


def _manifest_digest(body: dict[str, Any]) -> str:
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _manifest_paths(copy_root: Path, version: str) -> list[Path]:
    return sorted((copy_root / MANIFEST_DIRNAME).glob(f"snapshot-{version}-*.json"))


def write_initial_snapshot_manifest(*, canonical_path: Path, copy_path: Path,
                                    copy_root: Path, version: str,
                                    collection_name: str, quiescence_evidence: str,
                                    expected_record_count: int | None = None,
                                    inspector: Callable[[Path, str], dict[str, Any]] = inspect_collection_readonly
                                    ) -> Path:
    """Verify a quiescent initial copy and write a content-addressed read-only manifest."""
    if version not in {"v2", "v3"}:
        raise SnapshotVerificationError("snapshot version must be v2 or v3")
    if not quiescence_evidence:
        raise SnapshotVerificationError("initial copy requires evidence that writers were quiescent")
    manifest_dir = copy_root / MANIFEST_DIRNAME
    if _manifest_paths(copy_root, version):
        raise SnapshotVerificationError(f"an immutable {version} snapshot manifest already exists")

    canonical_hash_before = directory_sha256(canonical_path)
    copy_hash_before = directory_sha256(copy_path)
    source = inspector(canonical_path, collection_name)
    copied = inspector(copy_path, collection_name)
    canonical_hash_after = directory_sha256(canonical_path)
    copy_hash_after = directory_sha256(copy_path)
    if canonical_hash_before != canonical_hash_after or copy_hash_before != copy_hash_after:
        raise SnapshotVerificationError("database files changed during initial snapshot verification")
    if canonical_hash_before != copy_hash_before:
        raise SnapshotVerificationError("initial copy directory digest differs from canonical source")
    if source != copied:
        raise SnapshotVerificationError("initial copy collection identity, count, or metadata differs from source")
    if expected_record_count is not None and source.get("record_count") != expected_record_count:
        raise SnapshotVerificationError(
            f"source record count {source.get('record_count')} differs from expected {expected_record_count}"
        )

    body = {
        "manifest_version": MANIFEST_VERSION,
        "source_version": version,
        "verification_type": "initial_quiescent_copy",
        "source_path": str(canonical_path.resolve()),
        "snapshot_path": str(copy_path.resolve()),
        "collection": source,
        "source_directory_sha256": canonical_hash_before,
        "snapshot_directory_sha256": copy_hash_before,
        "quiescence_evidence": quiescence_evidence,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    manifest_sha = _manifest_digest(body)
    manifest = {**body, "manifest_sha256": manifest_sha}
    manifest_dir.mkdir(parents=True, exist_ok=True)
    destination = manifest_dir / f"snapshot-{version}-{manifest_sha}.json"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=manifest_dir,
                                         prefix=".snapshot.", suffix=".tmp", delete=False) as stream:
            temp_name = stream.name
            json.dump(manifest, stream, sort_keys=True, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o444)
        os.link(temp_name, destination)  # no overwrite, even in a concurrent invocation
        os.unlink(temp_name)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return destination


def load_snapshot_manifest(copy_root: Path, version: str) -> tuple[Path, dict[str, Any]]:
    candidates = _manifest_paths(copy_root, version)
    if len(candidates) != 1:
        raise SnapshotVerificationError(
            f"expected exactly one saved {version} snapshot manifest; found {len(candidates)}"
        )
    path = candidates[0]
    if path.stat().st_mode & 0o222:
        raise SnapshotVerificationError("snapshot manifest is writable; integrity protection is absent")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotVerificationError(f"snapshot manifest is unreadable: {exc}") from exc
    supplied_digest = manifest.get("manifest_sha256")
    body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if supplied_digest != _manifest_digest(body) or path.stem != f"snapshot-{version}-{supplied_digest}":
        raise SnapshotVerificationError("snapshot manifest integrity check failed")
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise SnapshotVerificationError("unsupported snapshot manifest version")
    return path, manifest


def validate_saved_snapshot(*, copy_root: Path, version: str, copy_path: Path,
                            collection_name: str, checkpoint: dict[str, Any] | None = None,
                            expected_record_count: int | None = None,
                            expected_collection_id: str | None = None,
                            allow_legacy_checkpoint: bool = True,
                            inspector: Callable[[Path, str], dict[str, Any]] = inspect_collection_readonly
                            ) -> dict[str, Any]:
    """Validate a temp snapshot without consulting the mutable current canonical DB."""
    copy_hash_before = directory_sha256(copy_path)
    actual = inspector(copy_path, collection_name)
    copy_hash_after = directory_sha256(copy_path)
    if copy_hash_before != copy_hash_after:
        raise SnapshotVerificationError("temporary database changed during read-only verification")
    if expected_record_count is not None and actual.get("record_count") != expected_record_count:
        raise SnapshotVerificationError("temporary collection record count differs from the expected source count")

    checkpoint_hash = ((checkpoint or {}).get("config") or {}).get("rag_snapshot_sha256")
    manifest_candidates = _manifest_paths(copy_root, version)
    manifest_path: Path | None = None
    manifest: dict[str, Any] | None = None
    if manifest_candidates:
        manifest_path, manifest = load_snapshot_manifest(copy_root, version)
    elif checkpoint is None or not allow_legacy_checkpoint:
        raise SnapshotVerificationError("no saved initial-copy manifest exists")
    if manifest is None:
        if checkpoint is None or not allow_legacy_checkpoint:
            raise SnapshotVerificationError("no saved initial-copy manifest exists")
        saved_config = checkpoint.get("config", {})
        saved_hash = saved_config.get("rag_snapshot_sha256")
        if not saved_hash or saved_hash != copy_hash_before:
            raise SnapshotVerificationError(
                "legacy checkpoint has no manifest and its pinned snapshot hash does not match the current copy "
                f"(checkpoint={saved_hash!r}, current={copy_hash_before!r})"
            )
        if saved_config.get("rag_version") != version:
            raise SnapshotVerificationError("legacy checkpoint RAG version does not match the temporary copy")
        if actual.get("collection_name") != collection_name:
            raise SnapshotVerificationError("legacy snapshot collection identity mismatch")
        if expected_collection_id and actual.get("collection_id") != expected_collection_id:
            raise SnapshotVerificationError("legacy snapshot collection UUID differs from the canonical collection")
        if expected_record_count is not None and actual.get("record_count") != expected_record_count:
            raise SnapshotVerificationError("legacy snapshot collection count differs from the canonical collection")
        return {
            "status": "legacy_checkpoint_hash_match",
            "safe_to_resume": True,
            "snapshot_directory_sha256": copy_hash_before,
            "checkpoint_directory_sha256": saved_hash,
            "collection": actual,
            "provenance": "initial_copy_provenance_not_established; checkpoint hash is the only saved byte identity",
            "manifest_path": None,
            "manifest_sha256": None,
        }

    # A valid saved manifest is now authoritative; the current canonical DB is deliberately not hashed here.
    assert manifest is not None and manifest_path is not None

    expected_collection = manifest.get("collection") or {}
    if manifest.get("source_version") != version:
        raise SnapshotVerificationError("snapshot manifest version does not match requested corpus")
    if Path(manifest.get("snapshot_path", "")).resolve() != copy_path.resolve():
        raise SnapshotVerificationError("snapshot manifest points to a different database path")
    if actual != expected_collection:
        raise SnapshotVerificationError("temporary collection identity/count differs from saved manifest")
    saved_hash = manifest.get("snapshot_directory_sha256")
    if copy_hash_before != saved_hash:
        raise SnapshotVerificationError("temporary database digest differs from saved snapshot manifest")
    if checkpoint is not None:
        config = checkpoint.get("config", {})
        if config.get("rag_snapshot_sha256") != saved_hash:
            raise SnapshotVerificationError("checkpoint snapshot digest differs from saved snapshot manifest")
        if config.get("rag_version") != version:
            raise SnapshotVerificationError("checkpoint RAG version differs from saved snapshot manifest")
    return {
        "status": "manifest_verified",
        "safe_to_resume": True,
        "snapshot_directory_sha256": copy_hash_before,
        "checkpoint_directory_sha256": checkpoint_hash,
        "collection": actual,
        "provenance": "initial_quiescent_copy_verified_by_saved_manifest",
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest["manifest_sha256"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create-initial-manifest", action="store_true")
    parser.add_argument("--canonical", type=Path)
    parser.add_argument("--copy", type=Path)
    parser.add_argument("--copy-root", type=Path)
    parser.add_argument("--version", choices=("v2", "v3"))
    parser.add_argument("--collection", required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--quiescence-evidence",
                        help="Caller assertion produced only after the copy script's writer guard passes")
    args = parser.parse_args()
    if not args.create_initial_manifest or not all((args.canonical, args.copy, args.copy_root,
                                                     args.version, args.quiescence_evidence)):
        parser.error("initial manifest creation requires canonical, copy, copy-root, version, and quiescence evidence")
    path = write_initial_snapshot_manifest(
        canonical_path=args.canonical, copy_path=args.copy, copy_root=args.copy_root,
        version=args.version, collection_name=args.collection,
        quiescence_evidence=args.quiescence_evidence,
        expected_record_count=args.expected_count,
    )
    print(json.dumps({"manifest": str(path), "verified": True}, indent=2))


if __name__ == "__main__":
    main()
