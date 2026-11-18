import hashlib

from dataset_pipeline.manifest import source_manifest


def test_source_manifest_metadata_and_optional_incremental_hash(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "source.jsonl"
    source.write_bytes(b'{"id":1}\n')
    cfg_path = root / "sources.yaml"
    cfg_path.write_text("fixture", encoding="utf8")
    cfg = {"project_root": ".", "source_revision": "fixture-v1",
           "microsoft": [{"path": "source.jsonl", "task": "comment", "split": "train"}]}

    metadata = source_manifest(cfg_path, cfg, root, checksum=False)
    assert metadata["sources"][0]["sha256"] is None
    assert metadata["sources"][0]["size_bytes"] == 9
    assert metadata["reconstruction_ready"] is False

    pinned = source_manifest(cfg_path, cfg, root, checksum=True)
    assert pinned["sources"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert pinned["reconstruction_ready"] is True
