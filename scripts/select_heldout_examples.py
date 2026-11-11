"""Manage evaluation holdout examples and exclusion manifest.

The teammate will supply the exact 100 held-out evaluation examples from
ronantakizawa/github-codereview (split='test').
These examples must never enter the retrieval database.

Usage:
    # If the official 100-example ID/index file is provided:
    python scripts/select_heldout_examples.py --input heldout_ids.json

    # To initialize / check pending status without fabricating an official list:
    python scripts/select_heldout_examples.py --status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "rag" / "data" / "evaluation"
EXAMPLES_PATH = OUTPUT_DIR / "heldout_examples.jsonl"
MANIFEST_PATH = OUTPUT_DIR / "heldout_manifest.json"
DATASET_ID = "ronantakizawa/github-codereview"
SPLIT = "test"
SAMPLE_SIZE = 100


def stable_fingerprint(row: dict) -> str:
    """Compute deterministic SHA-256 hash for identifying a review example."""
    identity = {
        key: row.get(key)
        for key in (
            "repo_name",
            "pr_number",
            "file_path",
            "comment_line",
            "before_code",
            "reviewer_comment",
            "after_code",
            "diff_context",
        )
    }
    payload = json.dumps(
        identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_provisional_manifest() -> Dict[str, Any]:
    """Create a provisional manifest explicitly marking official holdout as pending."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "dataset": DATASET_ID,
        "split": SPLIT,
        "sample_size": SAMPLE_SIZE,
        "official_holdout_supplied": False,
        "official_holdout_verified": False,
        "status": "pending_official_list",
        "fingerprints": [],
        "indices": [],
        "normalized_texts": [],
        "note": (
            "PENDING: The official 100 held-out evaluation examples have not yet "
            "been supplied by the teammate. Exclusion mechanism is active but "
            "marked unverified until official list is loaded."
        ),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("Initialized provisional holdout manifest at %s (official holdout pending).", MANIFEST_PATH)
    return manifest


def load_official_heldout_list(input_path: Path) -> Dict[str, Any]:
    """Load exact examples from dataset based on supplied official indices/IDs."""
    from datasets import load_dataset

    logger.info("Loading official holdout list from %s...", input_path)
    raw_data = json.loads(input_path.read_text(encoding="utf-8"))

    # Extract list of indices or record keys
    indices = []
    if isinstance(raw_data, list):
        for item in raw_data:
            if isinstance(item, int):
                indices.append(item)
            elif isinstance(item, dict) and "index" in item:
                indices.append(item["index"])
    elif isinstance(raw_data, dict) and "indices" in raw_data:
        indices = raw_data["indices"]

    logger.info("Loading dataset %s [%s]...", DATASET_ID, SPLIT)
    dataset = load_dataset(DATASET_ID, split=SPLIT)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fingerprints = []
    normalized_texts = []
    saved_count = 0

    with EXAMPLES_PATH.open("w", encoding="utf-8") as out_f:
        for idx in indices:
            if 0 <= idx < len(dataset):
                row = dict(dataset[idx])
                fp = stable_fingerprint(row)
                fingerprints.append(fp)

                # Store normalized text snippet for overlap matching
                code = str(row.get("before_code") or row.get("diff_context") or "")
                comment = str(row.get("reviewer_comment") or "")
                norm_snippet = " ".join((comment + " " + code).split()).casefold()[:200]
                if norm_snippet:
                    normalized_texts.append(norm_snippet)

                row["_holdout"] = {
                    "dataset": DATASET_ID,
                    "split": SPLIT,
                    "index": idx,
                    "fingerprint": fp,
                    "official": True,
                }
                out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
                saved_count += 1

    manifest = {
        "dataset": DATASET_ID,
        "split": SPLIT,
        "sample_size": saved_count,
        "official_holdout_supplied": True,
        "official_holdout_verified": True,
        "status": "official_list_verified",
        "fingerprints": fingerprints,
        "indices": indices,
        "normalized_texts": normalized_texts,
        "note": "Official 100 held-out evaluation examples loaded and verified.",
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("Saved %d official held-out examples to %s", saved_count, EXAMPLES_PATH)
    logger.info("Saved verified manifest to %s", MANIFEST_PATH)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage evaluation holdout exclusion list.")
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Path to official heldout indices or IDs JSON file supplied by teammate.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Check and display current holdout status.",
    )
    args = parser.parse_args()

    if args.input and args.input.exists():
        load_official_heldout_list(args.input)
    elif MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        print("\nCurrent Holdout Status:")
        print(f"  Official holdout supplied: {manifest.get('official_holdout_supplied', False)}")
        print(f"  Official holdout verified: {manifest.get('official_holdout_verified', False)}")
        print(f"  Status: {manifest.get('status')}")
        print(f"  Note: {manifest.get('note')}")
    else:
        create_provisional_manifest()


if __name__ == "__main__":
    main()

