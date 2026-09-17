from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional
import re


@dataclass
class CanonicalRecord:
    """
    Canonical Document Schema for RAG Ingestion.
    Standardizes record representations across different data sources (GitHub, Microsoft, internal).
    """
    id: str
    text: str
    source: str           # "github_codereview" | "microsoft_codereviewer" | "style_guide"
    language: str         # "python", "typescript", "cpp", "csharp", etc.
    type: str             # "review_comment" | "quality_estimation" | "code_refinement" | "convention"
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert canonical record instance to dictionary."""
        return asdict(self)


def sanitize_text(text: str) -> str:
    """
    Normalize text while preserving code formatting.
    Strips excessive surrounding whitespace and cleans unprintable control characters.
    """
    if not text:
        return ""
    # Strip null bytes & control chars except newline (\n) and tab (\t)
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    return cleaned.strip()


def validate_canonical_record(record: CanonicalRecord) -> bool:
    """
    Validation check: Record must contain non-empty ID and text > 10 characters.
    """
    if not record.id or not record.text:
        return False
    if len(record.text.strip()) < 10:
        return False
    return True
