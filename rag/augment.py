import logging
from typing import List

logger = logging.getLogger(__name__)


def clean_retrieved_snippets(retrieved_context: List[str]) -> List[str]:
    """
    Deduplicate and clean retrieved context snippets for augmentation.

    Args:
        retrieved_context: List of raw context strings returned by retrieval.

    Returns:
        List of cleaned, unique context snippet strings.
    """
    if not retrieved_context:
        return []

    cleaned = []
    seen = set()

    for item in retrieved_context:
        if not item:
            continue
        text = str(item).strip()
        if len(text) < 10:
            continue

        # Simple deduplication based on text fingerprint
        fingerprint = text.lower()[:150]
        if fingerprint in seen:
            continue

        seen.add(fingerprint)
        cleaned.append(text)

    return cleaned


def augment_context(code: str, retrieved_context: List[str]) -> str:
    """
    Augment current code or PR diff string with top-k retrieved RAG context snippets
    into a structured context prompt block for downstream LLM analysis.

    AUGMENTATION CONTRACT:
    - 100% deterministic (no LLM, no randomness, no network API calls).
    - Preserves exact code formatting and reviewer feedback.
    - Handles empty or duplicate retrieval outputs safely.

    Args:
        code: Current target source code or PR diff string.
        retrieved_context: List of retrieved context snippet strings.

    Returns:
        Formatted augmented context prompt string.
    """
    clean_code = str(code).strip() if code else "# (No code input provided)"
    snippets = clean_retrieved_snippets(retrieved_context)

    augmented_parts = [
        "# CURRENT CODE / PR DIFF",
        "```",
        clean_code,
        "```",
        ""
    ]

    if snippets:
        augmented_parts.append("# RELEVANT HISTORICAL CODE REVIEW CONTEXT")
        for idx, snippet in enumerate(snippets, start=1):
            augmented_parts.append(f"[Example {idx}]")
            augmented_parts.append(snippet)
            augmented_parts.append("")
    else:
        augmented_parts.append("# RELEVANT HISTORICAL CODE REVIEW CONTEXT")
        augmented_parts.append("(No additional relevant historical review context retrieved.)")
        augmented_parts.append("")

    augmented_parts.extend([
        "INSTRUCTION FOR DOWNSTREAM LLM:",
        "Analyze the CURRENT CODE independently using the RELEVANT HISTORICAL CODE REVIEW CONTEXT as reference material."
    ])

    return "\n".join(augmented_parts)
