"""Download a bounded Microsoft CodeReviewer Comment_Generation training sample.

The Zenodo Comment_Generation archive is large (about 847 MB). The script
streams only the first 8,000 valid training records into the local RAG source
file; it does not extract the full multi-gigabyte training split.

Run:
    python scripts/fetch_microsoft_codereviewer.py
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "rag" / "data" / "raw"
TARGET = RAW / "microsoft_codereviewer" / "Comment_Generation"
RECORD_API = "https://zenodo.org/api/records/6900648"
EXPECTED_FILE = "Comment_Generation.zip"
MAX_TRAIN_RECORDS = 8_000


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
        raise RuntimeError(f"{EXPECTED_FILE} was not found in Zenodo record 6900648.")

    download_url = file_info["links"]["self"]
    archive_path = RAW / "microsoft_codereviewer" / EXPECTED_FILE
    if not archive_path.exists() or archive_path.stat().st_size != file_info.get("size"):
        archive_path = RAW / EXPECTED_FILE

    expected_md5 = file_info.get("checksum", "").removeprefix("md5:")
    if not archive_path.exists() or archive_path.stat().st_size != file_info.get("size"):
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

        if expected_md5 and digest.hexdigest() != expected_md5:
            archive_path.unlink(missing_ok=True)
            raise RuntimeError("Downloaded archive failed its MD5 checksum.")
    else:
        print(f"Using existing archive: {archive_path}")
        # Verify checksum of existing archive
        digest = hashlib.md5()
        with archive_path.open("rb") as f:
            while block := f.read(1024 * 1024):
                digest.update(block)
        if expected_md5 and digest.hexdigest() != expected_md5:
            raise RuntimeError("Existing archive failed its MD5 checksum.")
        print(f"Checksum verified: {expected_md5}")

    output_path = TARGET / "msg-train-selected.jsonl"
    selected = 0
    with zipfile.ZipFile(archive_path) as archive, output_path.open("w", encoding="utf-8") as output:
        members = sorted(
            name for name in archive.namelist()
            if Path(name).name.startswith("msg-train") and name.endswith(".jsonl")
        )
        if not members:
            raise RuntimeError("No msg-train*.jsonl files found in the archive.")

        for member in members:
            with archive.open(member) as source:
                for raw_line in source:
                    if selected >= MAX_TRAIN_RECORDS:
                        break
                    try:
                        row = json.loads(raw_line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not row.get("msg") or not row.get("patch"):
                        continue
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                    selected += 1
            if selected >= MAX_TRAIN_RECORDS:
                break

    if selected == 0:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("No valid training records were extracted.")

    print(f"Selected {selected:,} valid training records.")
    print(f"Saved to {output_path.relative_to(ROOT)}")
    print("No validation or test rows were copied into the RAG source directory.")


if __name__ == "__main__":
    main()
