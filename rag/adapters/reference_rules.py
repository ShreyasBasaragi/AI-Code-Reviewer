"""Adapters for official OWASP/CWE and style/static-analysis references."""
from __future__ import annotations

import ast
import logging
import re
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable

from rag.adapters.base import CanonicalRecord, sanitize_text
from rag.config import RAW_DATA_DIR

logger = logging.getLogger(__name__)

CWE_ZIP = RAW_DATA_DIR / "cwe" / "cwec_latest.xml.zip"
OWASP_DIR = RAW_DATA_DIR / "owasp_cheatsheets"
BANDIT_DIR = RAW_DATA_DIR / "bandit_rules"
PEP8_HTML = RAW_DATA_DIR / "pep-0008.html"
GOOGLE_STYLE_MD = RAW_DATA_DIR / "google_pyguide.md"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_text(element) -> str:
    return sanitize_text(" ".join(part.strip() for part in element.itertext() if part.strip()))


def load_cwe_records(limit: int = 700) -> list[CanonicalRecord]:
    """Read weakness definitions from the official MITRE CWE XML archive."""
    if not CWE_ZIP.exists():
        logger.warning("CWE archive missing. Run scripts/fetch_reference_sources.py.")
        return []

    records = []
    with zipfile.ZipFile(CWE_ZIP) as archive:
        xml_names = [name for name in archive.namelist() if name.lower().endswith(".xml")]
        if not xml_names:
            raise ValueError(f"No XML file found in {CWE_ZIP}")
        with archive.open(xml_names[0]) as stream:
            import xml.etree.ElementTree as ET
            root = ET.parse(stream).getroot()

    for weakness in (node for node in root.iter() if _local_name(node.tag) == "Weakness"):
        cwe_id = weakness.attrib.get("ID")
        name = weakness.attrib.get("Name", "")
        description = ""
        extended = ""
        mitigations = []

        for child in weakness.iter():
            tag = _local_name(child.tag)
            if tag == "Description" and not description:
                description = _xml_text(child)
            elif tag == "Extended_Description" and not extended:
                extended = _xml_text(child)
            elif tag == "Mitigation":
                text = _xml_text(child)
                if text:
                    mitigations.append(text)

        if not cwe_id or not name or not description:
            continue

        sections = [
            f"Security weakness CWE-{cwe_id}: {name}",
            f"Description: {description}",
        ]
        if extended:
            sections.append(f"Additional detail: {extended}")
        if mitigations:
            sections.append("Potential mitigations: " + " ".join(mitigations[:3]))

        records.append(
            CanonicalRecord(
                id=f"cwe_{cwe_id}",
                text="\n".join(sections),
                source="owasp_cwe",
                language="language_agnostic",
                type="security_pattern",
                tags=["security", "cwe", "weakness"],
                metadata={
                    "cwe_id": str(cwe_id),
                    "weakness_name": name,
                    "source_url": "https://cwe.mitre.org/data/definitions/",
                    "license_note": "MITRE CWE terms of use apply",
                },
            )
        )
        if len(records) >= limit:
            break

    return records


def _markdown_sections(path: Path) -> Iterable[tuple[str, str]]:
    content = path.read_text(encoding="utf-8", errors="ignore")
    matches = list(re.finditer(r"(?m)^#{1,4}\s+(.+)$", content))
    if not matches:
        if content.strip():
            yield path.stem, content.strip()
        return

    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        title = match.group(1).strip()
        body = content[start:end].strip()
        if body:
            yield title, body


def load_owasp_records(limit: int = 250) -> list[CanonicalRecord]:
    """Convert OWASP Cheat Sheet markdown sections into source records."""
    if not OWASP_DIR.exists():
        logger.warning("OWASP source directory missing. Run scripts/fetch_reference_sources.py.")
        return []

    records = []
    markdown_files = sorted(OWASP_DIR.rglob("*.md"))
    for path in markdown_files:
        if any(part.startswith(".") for part in path.relative_to(OWASP_DIR).parts):
            continue
        for section_index, (title, body) in enumerate(_markdown_sections(path)):
            text = f"OWASP security guidance: {path.stem} — {title}\n{body}"
            if len(text) < 80:
                continue
            records.append(
                CanonicalRecord(
                    id=f"owasp_{path.stem}_{section_index}",
                    text=text,
                    source="owasp_cwe",
                    language="language_agnostic",
                    type="security_pattern",
                    tags=["security", "owasp", "secure_coding"],
                    metadata={
                        "origin": "OWASP Cheat Sheet Series",\n                        "cheatsheet": path.stem,
                        "section": title,
                        "source_url": "https://cheatsheetseries.owasp.org/",
                        "license": "CC BY-SA 4.0",
                    },
                )
            )
            if len(records) >= limit:
                return records
    return records


class _TextBlocks(HTMLParser):
    """Extract heading and paragraph-like text from PEP HTML."""

    def __init__(self):
        super().__init__()
        self.blocks = []
        self._parts = []
        self._tag = None

    def handle_starttag(self, tag, attrs):
        if tag in {"h1", "h2", "h3", "h4", "p", "li", "pre"}:
            self._flush()
            self._tag = tag

    def handle_endtag(self, tag):
        if tag == self._tag:
            self._flush()
            self._tag = None

    def handle_data(self, data):
        if self._tag:
            value = data.strip()
            if value:
                self._parts.append(value)

    def _flush(self):
        text = sanitize_text(" ".join(self._parts))
        if text:
            self.blocks.append((self._tag, text))
        self._parts = []


def _style_records_from_blocks(blocks, source: str, prefix: str, limit: int):
    records = []
    current_title = source
    for index, (tag, text) in enumerate(blocks):
        if tag in {"h1", "h2", "h3", "h4"}:
            current_title = text
            continue
        if len(text) < 45:
            continue
        records.append(
            CanonicalRecord(
                id=f"{prefix}_{index}",
                text=f"{source} rule — {current_title}\n{text}",
                source="style_static_rules",
                language="python",
                type="style_rule",
                tags=["style", "static_analysis", prefix],
                metadata={"rule_section": current_title, "source": source},
            )
        )
        if len(records) >= limit:
            break
    return records


def load_style_records(limit: int = 400) -> list[CanonicalRecord]:
    """Load PEP 8, Google Python Style Guide, and Bandit rule descriptions."""
    records = []

    if PEP8_HTML.exists():
        parser = _TextBlocks()
        parser.feed(PEP8_HTML.read_text(encoding="utf-8", errors="ignore"))
        records.extend(
            _style_records_from_blocks(parser.blocks, "PEP 8", "pep8", limit)
        )

    if len(records) < limit and GOOGLE_STYLE_MD.exists():
        blocks = [("p", body) for _, body in _markdown_sections(GOOGLE_STYLE_MD)]
        records.extend(
            _style_records_from_blocks(
                blocks, "Google Python Style Guide", "google_style", limit - len(records)
            )
        )

    if len(records) < limit and BANDIT_DIR.exists():
        plugin_files = sorted(BANDIT_DIR.rglob("*.py"))
        for path in plugin_files:
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                doc = ast.get_docstring(node)
                if not doc or len(doc.strip()) < 45:
                    continue
                records.append(
                    CanonicalRecord(
                        id=f"bandit_{path.stem}_{node.name}",
                        text=f"Bandit static-analysis rule {node.name}\n{doc.strip()}",
                        source="style_static_rules",
                        language="python",
                        type="static_analysis_rule",
                        tags=["bandit", "security_lint", "static_analysis"],
                        metadata={
                            "rule": node.name,
                            "source_file": path.name,
                            "source_url": "https://github.com/PyCQA/bandit",
                        },
                    )
                )
                if len(records) >= limit:
                    return records

    # Stable de-duplication across guides that may repeat the same advice.
    unique = []
    seen = set()
    for record in records:
        key = re.sub(r"\s+", " ", record.text).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
        if len(unique) >= limit:
            break
    return unique


def load_reference_rule_records() -> list[CanonicalRecord]:
    """Load the configured security and style/static-analysis source records."""
    return load_cwe_records() + load_owasp_records() + load_style_records()
