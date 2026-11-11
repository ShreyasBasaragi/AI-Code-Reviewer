
import json
import hashlib
from pathlib import Path
from collections import defaultdict

from datasets import get_dataset_split_names, load_dataset

DATASET_ID = "ronantakizawa/github-codereview"
SELECTION_FILE = Path("heldout_ids.json")
OUTPUT_FILE = Path("heldout_examples.jsonl")


def get_row_id(row):
    identity = {
        "repo_name": row.get("repo_name"),
        "pr_number": row.get("pr_number"),
        "file_path": row.get("file_path"),
        "comment_line": row.get("comment_line"),
        "before_code": row.get("before_code"),
        "reviewer_comment": row.get("reviewer_comment"),
        "after_code": row.get("after_code"),
    }

    serialized = json.dumps(
        identity, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def inspect_dataset():
    splits = get_dataset_split_names(DATASET_ID)

    print("Available splits:")
    for split in splits:
        dataset = load_dataset(DATASET_ID, split=split)
        print(f"{split}: {len(dataset):,} rows")
        print("Columns:", dataset.column_names)

    return splits


def fetch_selected_examples():
    if not SELECTION_FILE.exists():
        print(f"\nCreate {SELECTION_FILE} using the format below.")
        return

    selections = json.loads(SELECTION_FILE.read_text())
    grouped = defaultdict(list)

    for item in selections:
        grouped[item["split"]].append(item["index"])

    count = 0

    with OUTPUT_FILE.open("w", encoding="utf-8") as output:
        for split, indices in grouped.items():
            dataset = load_dataset(DATASET_ID, split=split)

            for index in indices:
                if index < 0 or index >= len(dataset):
                    raise IndexError(f"Invalid index: {split}[{index}]")

                row = dict(dataset[index])
                row["dataset_split"] = split
                row["dataset_index"] = index
                row["row_id"] = get_row_id(row)

                output.write(
                    json.dumps(row, ensure_ascii=False) + "\n"
                )
                count += 1

    print(f"\nExported {count} examples to {OUTPUT_FILE}")


if __name__ == "__main__":
    inspect_dataset()
    fetch_selected_examples()
