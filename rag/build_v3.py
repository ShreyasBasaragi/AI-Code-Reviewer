"""Build and validate an isolated ChromaDB v3 database from prepared artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rag.config import PROJECT_ROOT
from rag.v3.build import BuildError, build_v3, validate_prepared_artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--prepared-dir", type=Path,
                        help="Prepared v3 artifact directory; defaults to rag/data/processed/v3_preparation")
    parser.add_argument("--batch-size", type=int, default=128,
                        help="Bounded embedding and Chroma insertion batch size")
    parser.add_argument("--validate-only", action="store_true",
                        help="Validate prepared artifacts without loading embeddings or opening ChromaDB")
    args = parser.parse_args(argv)
    prepared_dir = args.prepared_dir or args.project_root / "rag" / "data" / "processed" / "v3_preparation"
    try:
        if args.validate_only:
            summary = validate_prepared_artifacts(prepared_dir)
            output = {
                "prepared_artifacts_valid": True,
                "parent_count": len(summary.parents),
                "chunk_count": summary.chunk_count,
                "parent_counts_by_source": dict(sorted(summary.parent_counts.items())),
                "chunk_counts_by_source": dict(sorted(summary.chunks_by_source.items())),
                "unknown_language_parent_count": sum(
                    1 for parent in summary.parents.values() if parent.language == "unknown"
                ),
                "chromadb_opened": False,
                "embeddings_generated": False,
            }
        else:
            result = build_v3(project_root=args.project_root, prepared_dir=prepared_dir,
                              batch_size=args.batch_size)
            output = {
                "promoted": result["promoted"],
                "final_directory": result.get("final_directory"),
                "staging_directory": result.get("staging_directory"),
                "build_manifest": result["build_manifest"],
                "validation_report": result["validation_report"],
            }
    except (BuildError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(output, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
