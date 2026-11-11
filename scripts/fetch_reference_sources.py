"""Download the official source material used by the v2 RAG rule corpus.

Run from the repository root:
    python scripts/fetch_reference_sources.py

Downloads are stored under rag/data/raw and are intentionally gitignored.
The sources are MITRE CWE, OWASP Cheat Sheet Series, PEP 8, Google Python
Style Guide, and Bandit's public plugin rules.
"""
from __future__ import annotations

import shutil
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "rag" / "data" / "raw"
DOWNLOADS = {
    "cwe": "https://cwe.mitre.org/data/xml/cwec_latest.xml.zip",
    "owasp": "https://github.com/OWASP/CheatSheetSeries/archive/refs/heads/master.zip",
    "bandit": "https://github.com/PyCQA/bandit/archive/refs/heads/main.zip",
    "pep8": "https://peps.python.org/pep-0008/",
    "google_style": "https://raw.githubusercontent.com/google/styleguide/gh-pages/pyguide.md",
}


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url, headers={"User-Agent": "Critique-RAG-Corpus-Builder/2.0"}
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        destination.write_bytes(response.read())
    print(f"Downloaded {destination.relative_to(ROOT)}")


def extract_zip(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zipped:
        zipped.extractall(destination)
    print(f"Extracted {archive.name} -> {destination.relative_to(ROOT)}")


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)

    cwe_zip = RAW / "cwe" / "cwec_latest.xml.zip"
    owasp_zip = RAW / "owasp_cheatsheets.zip"
    bandit_zip = RAW / "bandit_rules.zip"

    download(DOWNLOADS["cwe"], cwe_zip)
    download(DOWNLOADS["owasp"], owasp_zip)
    download(DOWNLOADS["bandit"], bandit_zip)
    download(DOWNLOADS["pep8"], RAW / "pep-0008.html")
    download(DOWNLOADS["google_style"], RAW / "google_pyguide.md")

    extract_zip(owasp_zip, RAW / "owasp_cheatsheets")
    extract_zip(bandit_zip, RAW / "bandit_rules")

    print("\nSource download complete.")
    print("Review source licenses/attribution before redistributing derived material.")
    print("The raw downloads are local inputs and should not be committed.")


if __name__ == "__main__":
    main()
