"""Download the Microsoft CodeReviewer Comment_Generation training data.

The complete Comment_Generation archive is large (roughly 847 MB according to
the Zenodo record). Only msg-train*.jsonl files are copied into the local RAG
source directory; validation and test files are deliberately ignored.

Run:
    python scripts/fetch_microsoft_codereviewer.py
"""
from __future__ import annotations

import hashlib
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "rag" / "data" / "raw"
TARGET = RAW / "microsoft_codereviewer" / "Comment_Generation"
RECORD_API = "https://zenodo.org/api/records/6900648"
EXPECTED_FILE = "Comment_Generation.zip"


def main() -> None:
    TARGET.mkdir(parents=True, exist_ok=True)

    request = urllib.request.Request(
        RECORD_API, headers={"User-Agent": "Critique-RAG-Corpus-Builder/2.0"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        record = json.load(response)

    file_info = next(
        (item for item in record.get("files", []) if item.get("key") == EXPECTED_FILE),
        None,
    )
    if file_info is None:
        raise RuntimeError(
            f"{EXPECTED_FILE} was not found in Zenodo record 6900648. "
            "Inspect the record metadata before proceeding."
        )

    download_url = file_info["links"]["self"]
    archive_path = RAW / EXPECTED_FILE
    print(
        f"Downloading {EXPECTED_FILE} "
        f"({file_info.get('size', 0) / (1024 * 1024):.1f} MB)..."
    )

    request = urllib.request.Request(
        download_url, headers={"User-Agent": "Critique-RAG-Corpus-Builder/2.0"}
    )
    digest = hashlib.md5()
    with urllib.request.urlopen(request, timeout=180) as response, archive_path.open("wb") as output:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            output.write(block)
            digest.update(block)

    expected_md5 = file_info.get("checksum", "").removeprefix("md5:")
    if expected_md5 and digest.hexdigest() != expected_md5:
        archive_path.unlink(missing_ok=True)
        raise RuntimeError("Downloaded archive failed its MD5 checksum.")

    print("Extracting only training JSONL files...")
    copied = 0
    with zipfile.ZipFile(archive_path) as archive:
        train_members = [
            name for name in archive.namelist()
            if Path(name).name.startswith("msg-train")
            and name.endswith(".jsonl")
        ]
        if not train_members:
            raise RuntimeError(
                "No msg-train*.jsonl files found. Inspect the archive layout."
            )
        for member in train_members:
            destination = TARGET / Path(member).name
            with archive.open(member) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
            copied += 1

    print(f"Copied {copied} training file(s) to {TARGET.relative_to(ROOT)}")
    print("Validation/test files were not copied into the RAG source directory.")
    print("The original archive remains under rag/data/raw and is gitignored.")


if __name__ == "__main__":
    main()
