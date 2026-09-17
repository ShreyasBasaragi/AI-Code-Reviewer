import logging
from typing import List, Optional, Dict, Any
from rag.adapters.base import CanonicalRecord, sanitize_text, validate_canonical_record

logger = logging.getLogger(__name__)


def load_github_codereview(
    split: str = "train",
    max_records: Optional[int] = 1000,
    min_quality_score: Optional[float] = None
) -> List[CanonicalRecord]:
    """
    Adapter for Hugging Face 'ronantakizawa/github-codereview' dataset.

    Args:
        split: Dataset split ('train', 'validation', 'test').
        max_records: Limit number of records loaded (for memory efficiency & testing).
        min_quality_score: Optional threshold to filter low-quality comments.

    Returns:
        List of CanonicalRecord objects.
    """
    try:
        from datasets import load_dataset
    except ImportError:
        logger.error("Hugging Face 'datasets' library is not installed. Run 'pip install datasets'.")
        return []

    logger.info(f"Loading Hugging Face dataset 'ronantakizawa/github-codereview' (split={split})...")
    try:
        dataset = load_dataset("ronantakizawa/github-codereview", split=split)
    except Exception as e:
        logger.error(f"Failed to load Hugging Face dataset 'ronantakizawa/github-codereview': {e}")
        return []

    records: List[CanonicalRecord] = []
    total_loaded = len(dataset)
    limit = max_records if max_records and max_records < total_loaded else total_loaded

    logger.info(f"Processing up to {limit} records from {total_loaded} total split rows...")

    for idx in range(limit):
        item = dataset[idx]

        # Inspect quality score if filtering enabled
        quality_score = item.get("quality_score")
        if min_quality_score is not None and quality_score is not None and float(quality_score) < min_quality_score:
            continue

        reviewer_comment = sanitize_text(item.get("reviewer_comment", ""))
        diff_context = sanitize_text(item.get("diff_context", ""))
        before_code = sanitize_text(item.get("before_code", ""))
        after_code = sanitize_text(item.get("after_code", ""))
        file_path = item.get("file_path", "")
        language = item.get("language") or item.get("repo_language") or "unknown"
        comment_type = item.get("comment_type", "general")
        repo_name = item.get("repo_name", "unknown")
        pr_number = item.get("pr_number")

        # Skip records missing both comment and diff/code
        if not reviewer_comment or (not diff_context and not before_code):
            continue

        # Format canonical text string for embedding
        text_parts = [
            f"GitHub Code Review Feedback ({language}):",
            f"Reviewer Comment: {reviewer_comment}"
        ]
        if file_path:
            text_parts.append(f"File: {file_path}")
        if diff_context:
            text_parts.append(f"Code Diff Context:\n{diff_context}")
        elif before_code:
            text_parts.append(f"Code Snippet:\n{before_code}")
        if after_code and after_code != before_code:
            text_parts.append(f"Suggested After Code:\n{after_code}")

        full_text = "\n".join(text_parts)
        record_id = f"github_{split}_{idx}"

        rec = CanonicalRecord(
            id=record_id,
            text=full_text,
            source="github_codereview",
            language=str(language).lower(),
            type="review_comment",
            tags=[str(language).lower(), str(comment_type).lower()],
            metadata={
                "repo_name": repo_name,
                "file_path": file_path,
                "pr_number": pr_number,
                "comment_type": comment_type,
                "quality_score": quality_score,
                "original_index": idx
            }
        )

        if validate_canonical_record(rec):
            records.append(rec)

    logger.info(f"Extracted {len(records)} canonical records from 'github-codereview'.")
    return records
