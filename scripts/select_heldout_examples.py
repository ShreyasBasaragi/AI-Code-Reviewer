"""Create a reproducible 100-example evaluation holdout.

The selected examples come only from the Hugging Face test split. The
github-codereview dataset is not an ingestion source for the v2 corpus.
Generated JSONL/JSON files are local evaluation artifacts and should not be
committed to Git.
"""
import hashlib
import json
import random
from pathlib import Path

from datasets import load_dataset

DATASET_ID = "ronantakizawa/github-codereview"
SPLIT = "test"
SAMPLE_SIZE = 100
RANDOM_SEED = 20261111
OUTPUT_DIR = Path("rag/data/evaluation")
EXAMPLES_PATH = OUTPUT_DIR / "heldout_examples.jsonl"
MANIFEST_PATH = OUTPUT_DIR / "heldout_manifest.json"


def stable_fingerprint(row: dict) -> str:
    """Hash the semantic fields used to identify a review example."""
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


def main() -> None:
    print(f"Loading {DATASET_ID} [{SPLIT}]...")
    dataset = load_dataset(DATASET_ID, split=SPLIT)
    if len(dataset) < SAMPLE_SIZE:
        raise RuntimeError(
            f"Expected at least {SAMPLE_SIZE} rows; found {len(dataset)}."
        )

    indices = list(range(len(dataset)))
    random.Random(RANDOM_SEED).shuffle(indices)
    selected_indices = sorted(indices[:SAMPLE_SIZE])

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fingerprints = []
    with EXAMPLES_PATH.open("w", encoding="utf-8") as output:
        for index in selected_indices:
            row = dict(dataset[index])
            fingerprint = stable_fingerprint(row)
            fingerprints.append(fingerprint)
            row["_holdout"] = {
                "dataset": DATASET_ID,
                "split": SPLIT,
                "index": index,
                "fingerprint": fingerprint,
                "seed": RANDOM_SEED,
            }
            output.write(json.dumps(row, ensure_ascii=False) + "\n")

    manifest = {
        "dataset": DATASET_ID,
        "split": SPLIT,
        "dataset_fingerprint": getattr(dataset, "_fingerprint", None),
        "sample_size": SAMPLE_SIZE,
        "random_seed": RANDOM_SEED,
        "indices": selected_indices,
        "fingerprints": fingerprints,
        "note": (
            "The source dataset is intentionally excluded from the v2 RAG "
            "ingestion pipeline. This holdout is reproducible for the same "
            "dataset revision and split."
        ),
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    print(f"Exported {SAMPLE_SIZE} held-out examples to {EXAMPLES_PATH}")
    print(f"Manifest written to {MANIFEST_PATH}")
    print("Do not add these generated evaluation files to Git.")


if __name__ == "__main__":
    main()
