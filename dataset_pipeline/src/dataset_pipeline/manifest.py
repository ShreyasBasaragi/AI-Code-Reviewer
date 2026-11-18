"""Deterministic file and source manifest helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def sha256_file(path: str | Path, chunk_bytes: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk_bytes), b""):
            digest.update(block)
    return digest.hexdigest()


def file_manifest(path: str | Path, *, checksum: bool = True) -> dict[str, Any]:
    p = Path(path)
    stat = p.stat()
    return {"path": str(p), "size_bytes": stat.st_size,
            "modified_time_ns": stat.st_mtime_ns,
            "sha256": sha256_file(p) if checksum else None,
            "checksum_scope": "entire_file" if checksum else "not_computed"}


def source_manifest(config_path: str | Path, config: dict[str, Any], root: str | Path,
                    *, checksum: bool = False) -> dict[str, Any]:
    """Describe configured sources; hashes are opt-in because they read full files."""
    root_path = Path(root)
    entries = []
    items = list(config.get("microsoft", []))
    if config.get("devign_candidate"):
        items.append(config["devign_candidate"])
    for item in items:
        path = Path(item["path"])
        path = path if path.is_absolute() else root_path / path
        entry: dict[str, Any] = {
            "configured_path": str(item["path"]),
            "path_relative_to_project_root": str(path.resolve().relative_to(root_path.resolve()))
                if path.exists() and path.resolve().is_relative_to(root_path.resolve()) else None,
            "exists": path.is_file(), "task": item.get("task", "devign_candidate"),
            "split": item.get("split"), "revision": config.get("source_revision", "unversioned-local"),
            "sha256": None, "checksum_status": "not_computed",
        }
        if path.is_file():
            stat = path.stat()
            entry.update({"size_bytes": stat.st_size, "modified_time_ns": stat.st_mtime_ns})
            if checksum:
                entry["sha256"] = sha256_file(path)
                entry["checksum_status"] = "computed_entire_file"
        else:
            entry.update({"size_bytes": None, "modified_time_ns": None,
                          "checksum_status": "source_missing"})
        entries.append(entry)
    cfg_path = Path(config_path)
    return {"manifest_version": "1.0", "source_revision": config.get("source_revision", "unversioned-local"),
            "configuration_path": cfg_path.name, "configuration_sha256": sha256_file(cfg_path),
            "checksum_mode": "full_file_sha256" if checksum else "metadata_only_no_source_reads",
            "sources": entries,
            "reconstruction_ready": bool(entries) and all(x["exists"] and x["sha256"] for x in entries),
            "note": "A source checksum requires one complete sequential read; metadata-only mode does not read source contents."}
