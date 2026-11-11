from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional
import hashlib
import re

VALID_SOURCES = {
    "microsoft_codereviewer",
    "owasp_cwe",
    "style_static_rules",
    "github_api",
    "github_codereview",
}


@dataclass
class CanonicalRecord:
    """
    Canonical Document Schema for RAG Ingestion.
    Standardizes record representations across different data sources:
    - Microsoft CodeReviewer (historical code reviews)
    - MITRE CWE + OWASP Cheat Sheets (security patterns)
    - PEP 8 + Google Python Style Guide + Bandit (style & static-analysis rules)
    """
    id: str
    text: str
    source: str           # "microsoft_codereviewer" | "owasp_cwe" | "style_static_rules" | "github_api"
    language: str         # "python", "language_agnostic", etc.
    type: str             # "review_comment" | "security_pattern" | "style_rule" | "static_analysis_rule"
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert canonical record instance to dictionary."""
        return asdict(self)


def sanitize_text(text: str) -> str:
    """
    Normalize text while strictly preserving code indentation and formatting.
    Strips unprintable control characters (except newline, carriage return, and tab)
    and removes outer whitespace.
    """
    if not text:
        return ""
    # Strip null bytes & control chars except \t (0x09), \n (0x0a), \r (0x0d)
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', str(text))
    return cleaned.strip()


def compute_content_hash(text: str) -> str:
    """Compute a deterministic SHA-256 hash of whitespace-normalized text."""
    normalized = " ".join(text.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def generate_record_id(source: str, identifier: str, content: Optional[str] = None) -> str:
    """Generate a deterministic, stable ID for a source record."""
    clean_id = re.sub(r'[^a-zA-Z0-9_\-.]', '_', str(identifier))
    if content:
        c_hash = compute_content_hash(content)[:10]
        return f"{source}_{clean_id}_{c_hash}"
    return f"{source}_{clean_id}"


def validate_canonical_record(record: CanonicalRecord) -> bool:
    """
    Validation check:
    - ID must be non-empty string.
    - Text must be non-empty with length >= 10.
    - Language and type must be non-empty strings.
    - Source must be non-empty string.
    """
    if not isinstance(record, CanonicalRecord):
        return False
    if not record.id or not isinstance(record.id, str):
        return False
    if not record.text or not isinstance(record.text, str):
        return False
    if len(record.text.strip()) < 10:
        return False
    if not record.source or not isinstance(record.source, str):
        return False
    if not record.language or not isinstance(record.language, str):
        return False
    if not record.type or not isinstance(record.type, str):
        return False
    return True

