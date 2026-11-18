"""Prepare v3 canonical records and lossless chunks without ChromaDB or embeddings.

Run ``python -m rag.prepare_v3 --dry-run`` for local-only validation. This module
never downloads source files, creates Chroma collections, or generates vectors.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from rag.config import RAW_DATA_DIR
from rag.v3.prepare import PreparationConfig, PreparedCorpus, prepare_local_sources
from rag.v3.sources import SOURCE_FAMILIES

ARTIFACT_NAMES = {
    "parents": "canonical_parents_v3.jsonl",
    "chunks": "embedding_chunks_v3.jsonl",
    "manifest": "preparation_manifest_v3.json",
    "rejections": "rejected_records_v3.jsonl",
    "duplicates": "duplicate_candidates_v3.jsonl",
}


def _load_tokenizer(name: str):
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("transformers is required to measure embedding-token counts") from exc
    try:
        return AutoTokenizer.from_pretrained(name, local_files_only=True)
    except Exception as exc:
        raise RuntimeError(
            f"Tokenizer {name!r} is not available in the local cache; no download was attempted"
        ) from exc


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _jsonl(rows: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for row in rows)


def write_artifacts(corpus: PreparedCorpus, output_dir: Path) -> dict[str, str]:
    """Atomically write each prepared artifact; manifest is written last."""
    output_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "parents": _jsonl([item.to_dict() for item in corpus.parents]),
        "chunks": _jsonl([item.to_dict() for item in corpus.chunks]),
        "rejections": _jsonl(corpus.rejected),
        "duplicates": _jsonl(corpus.duplicate_candidates),
    }
    for key in ("parents", "chunks", "rejections", "duplicates"):
        _atomic_text(output_dir / ARTIFACT_NAMES[key], payloads[key])
    manifest = dict(corpus.manifest)
    manifest["artifacts"] = {key: ARTIFACT_NAMES[key] for key in ARTIFACT_NAMES if key != "manifest"}
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _atomic_text(output_dir / ARTIFACT_NAMES["manifest"], manifest_text)
    return {key: str(output_dir / name) for key, name in ARTIFACT_NAMES.items()}


def _parse_parent_limit(value: str) -> tuple[str, int]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("parent limit must be SOURCE=COUNT")
    family, raw_count = value.split("=", 1)
    if family not in SOURCE_FAMILIES:
        raise argparse.ArgumentTypeError(f"unknown source family {family!r}")
    try:
        count = int(raw_count)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("parent limit count must be an integer") from exc
    if count < 0:
        raise argparse.ArgumentTypeError("parent limit count cannot be negative")
    return family, count


def run_preparation(
    *, input_root: Path, output_dir: Path, tokenizer, config: PreparationConfig,
    dry_run: bool = False,
) -> PreparedCorpus:
    corpus = prepare_local_sources(input_root=input_root, tokenizer=tokenizer, config=config)
    if not corpus.parents:
        corpus.manifest["warnings"] = list(corpus.manifest["warnings"]) + [
            "no eligible local source parents were prepared"
        ]
    if not dry_run:
        write_artifacts(corpus, output_dir)
    return corpus


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, default=RAW_DATA_DIR,
                        help="Root containing the existing local source directories")
    parser.add_argument("--output-dir", type=Path,
                        default=Path("rag/data/processed/v3_preparation"),
                        help="Directory for JSONL and manifest artifacts")
    parser.add_argument("--tokenizer-name", default="sentence-transformers/all-MiniLM-L6-v2",
                        help="Tokenizer identifier; loaded from local cache only")
    parser.add_argument("--max-chunk-tokens", type=int, default=200)
    parser.add_argument("--max-microsoft-source-records", type=int,
                        help="Optional source parse cap for smoke runs, before per-source allocation")
    parser.add_argument("--parent-limit", action="append", type=_parse_parent_limit, default=[],
                        metavar="SOURCE=COUNT",
                        help="Independent logical-parent limit; repeat for source families")
    parser.add_argument("--dry-run", action="store_true",
                        help="Prepare and report counts without writing any artifacts")
    args = parser.parse_args(argv)
    if args.max_microsoft_source_records is not None and args.max_microsoft_source_records < 0:
        parser.error("--max-microsoft-source-records cannot be negative")
    limits = dict(args.parent_limit)
    if len(limits) != len(args.parent_limit):
        parser.error("a source parent limit may be specified only once")
    config = PreparationConfig(
        max_chunk_tokens=args.max_chunk_tokens,
        parent_limits=limits,
        max_microsoft_source_records=args.max_microsoft_source_records,
        tokenizer_name=args.tokenizer_name,
    )
    try:
        config.validate()
        tokenizer = _load_tokenizer(args.tokenizer_name)
        corpus = run_preparation(
            input_root=args.input_root, output_dir=args.output_dir,
            tokenizer=tokenizer, config=config, dry_run=args.dry_run,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    report = {
        "dry_run": args.dry_run,
        "parent_count": len(corpus.parents),
        "chunk_count": len(corpus.chunks),
        "rejected_count": len(corpus.rejected),
        "duplicate_candidate_count": len(corpus.duplicate_candidates),
        "warnings": corpus.manifest["warnings"],
        "parent_counts_by_source": corpus.manifest["parent_counts_by_source"],
        "chunk_counts_by_source": corpus.manifest["chunk_counts_by_source"],
        "output_dir": None if args.dry_run else str(args.output_dir),
        "chromadb_touched": False,
        "embedding_generation_performed": False,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
