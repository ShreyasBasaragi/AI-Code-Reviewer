import logging
from typing import List, Dict, Any, Optional
from rag.adapters.base import CanonicalRecord, sanitize_text, validate_canonical_record

logger = logging.getLogger(__name__)


def _detect_language_from_path(file_path: str) -> str:
    """Detect programming language tag from file extension."""
    if not file_path or "." not in file_path:
        return "unknown"
    ext = file_path.rsplit(".", 1)[-1].lower()
    mapping = {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "jsx": "javascript",
        "cs": "csharp",
        "cpp": "cpp",
        "cc": "cpp",
        "c": "c",
        "h": "c",
        "hpp": "cpp",
        "java": "java",
        "go": "go",
        "rb": "ruby",
        "php": "php",
        "rs": "rust",
        "swift": "swift",
        "kt": "kotlin",
        "md": "markdown",
        "json": "json",
        "yml": "yaml",
        "yaml": "yaml"
    }
    return mapping.get(ext, ext)


def convert_github_api_pr_to_canonical(
    owner: str,
    repo: str,
    pr_info: Dict[str, Any],
    files: List[Dict[str, Any]],
    reviews: List[Dict[str, Any]],
    comments: List[Dict[str, Any]]
) -> List[CanonicalRecord]:
    """
    Convert raw GitHub REST API PR responses into standard CanonicalRecord objects.

    Distinguishes:
    - Review-oriented records (`type = 'code_review'`): Contains explicit reviewer feedback comments.
    - Raw PR file context (`type = 'pr_file_diff'`): Changed file patches without inline review comments.

    Args:
        owner: Repository owner.
        repo: Repository name.
        pr_info: PR metadata dictionary.
        files: List of PR changed file dicts.
        reviews: List of PR review dicts.
        comments: List of PR inline review comment dicts.

    Returns:
        List of CanonicalRecord objects.
    """
    records: List[CanonicalRecord] = []
    repo_slug = f"{owner}/{repo}"
    pr_number = pr_info.get("number") or pr_info.get("id", "unknown")
    pr_title = pr_info.get("title", "")

    file_patch_map = {f.get("filename"): f.get("patch", "") for f in files if f.get("filename")}

    # 1. Process inline line-level code review comments (Prioritized high-value feedback signal)
    for comment in comments:
        comment_id = comment.get("id")
        body = sanitize_text(comment.get("body", ""))
        file_path = comment.get("path", "")
        diff_hunk = sanitize_text(comment.get("diff_hunk", ""))
        user_login = comment.get("user", {}).get("login", "unknown")

        if not body or not file_path:
            continue

        language = _detect_language_from_path(file_path)
        file_patch = file_patch_map.get(file_path, "")
        code_context = diff_hunk if diff_hunk else file_patch

        text_parts = [
            f"Live GitHub Code Review ({repo_slug} PR #{pr_number}):",
            f"PR Title: {pr_title}",
            f"Reviewer Comment: {body}",
            f"File: {file_path}"
        ]
        if code_context:
            text_parts.append(f"Code Diff Context:\n{code_context}")

        full_text = "\n".join(text_parts)
        # Deterministic ID based on GitHub comment ID
        record_id = f"github_api_{owner}_{repo}_pr{pr_number}_comment_{comment_id}"

        rec = CanonicalRecord(
            id=record_id,
            text=full_text,
            source="github_api",
            language=language,
            type="code_review",
            tags=[language, "github_api", f"pr_{pr_number}"],
            metadata={
                "repository": repo_slug,
                "pr_number": pr_number,
                "pr_title": pr_title,
                "file_path": file_path,
                "comment_id": comment_id,
                "reviewer": user_login
            }
        )

        if validate_canonical_record(rec):
            records.append(rec)

    # 2. Process file diff patches that have PR metadata if no inline comment exists
    if not comments and files:
        for f in files:
            filename = f.get("filename", "")
            patch = sanitize_text(f.get("patch", ""))
            if not filename or not patch or len(patch) < 15:
                continue

            language = _detect_language_from_path(filename)
            text_parts = [
                f"Live GitHub PR File Diff ({repo_slug} PR #{pr_number}):",
                f"PR Title: {pr_title}",
                f"File: {filename}",
                f"Code Patch:\n{patch}"
            ]

            full_text = "\n".join(text_parts)
            # Deterministic ID based on file path slug
            path_slug = filename.replace("/", "_").replace(".", "_")
            record_id = f"github_api_{owner}_{repo}_pr{pr_number}_file_{path_slug}"

            rec = CanonicalRecord(
                id=record_id,
                text=full_text,
                source="github_api",
                language=language,
                type="pr_file_diff",  # Distinguish raw PR file patch from explicit code review feedback
                tags=[language, "github_api", f"pr_{pr_number}", "file_diff"],
                metadata={
                    "repository": repo_slug,
                    "pr_number": pr_number,
                    "pr_title": pr_title,
                    "file_path": filename
                }
            )

            if validate_canonical_record(rec):
                records.append(rec)

    logger.info(f"Converted GitHub API PR #{pr_number} payload into {len(records)} canonical records.")
    return records
