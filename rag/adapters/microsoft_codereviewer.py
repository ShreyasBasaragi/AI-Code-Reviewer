import json
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any

from rag.adapters.base import CanonicalRecord, sanitize_text, validate_canonical_record
from rag.config import RAW_DATA_DIR

logger = logging.getLogger(__name__)

MS_CODEREVIEWER_DIR = RAW_DATA_DIR / "microsoft_codereviewer"


def _map_language(lang_ext: Optional[str]) -> str:
    """Map language file extension or string to standard language tag."""
    if not lang_ext:
        return "unknown"
    ext_clean = str(lang_ext).strip().lstrip(".").lower()
    mapping = {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "cs": "csharp",
        "cpp": "cpp",
        "c": "c",
        "java": "java",
        "go": "go",
        "rb": "ruby",
        "php": "php"
    }
    return mapping.get(ext_clean, ext_clean)


def parse_comment_generation_files(dir_path: Path, max_records: Optional[int] = 500) -> List[CanonicalRecord]:
    """Parse Comment_Generation JSONL files (msg-*.jsonl)."""
    records: List[CanonicalRecord] = []
    if not dir_path.exists():
        return records

    jsonl_files = sorted(dir_path.glob("msg-valid.jsonl")) + sorted(dir_path.glob("msg-test.jsonl")) + sorted(dir_path.glob("msg-train*.jsonl"))

    for file_path in jsonl_files:
        logger.info(f"Parsing Microsoft Comment_Generation file: {file_path.name}")
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line_idx, line in enumerate(f):
                if max_records and len(records) >= max_records:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                patch = sanitize_text(data.get("patch", ""))
                msg = sanitize_text(data.get("msg", ""))
                oldf = sanitize_text(data.get("oldf", ""))
                proj = data.get("proj", "unknown")
                lang = _map_language(data.get("lang"))
                # Use file stem + line index to guarantee globally unique ID
                rec_id = f"ms_msg_{file_path.stem}_{line_idx}"

                if not msg or not patch:
                    continue

                text_parts = [
                    f"Microsoft CodeReviewer Feedback ({lang}):",
                    f"Reviewer Comment: {msg}",
                    f"Project: {proj}",
                    f"Code Diff Patch:\n{patch}"
                ]
                if oldf and len(oldf) < 1500:
                    text_parts.append(f"Original Code:\n{oldf[:1000]}")

                full_text = "\n".join(text_parts)

                rec = CanonicalRecord(
                    id=rec_id,
                    text=full_text,
                    source="microsoft_codereviewer",
                    language=lang,
                    type="comment_generation",
                    tags=[lang, "comment_generation"],
                    metadata={
                        "project": proj,
                        "original_id": data.get("id"),
                        "category": "Comment_Generation",
                        "file_source": file_path.name
                    }
                )

                if validate_canonical_record(rec):
                    records.append(rec)

        if max_records and len(records) >= max_records:
            break

    return records


def parse_diff_quality_files(dir_path: Path, max_records: Optional[int] = 500) -> List[CanonicalRecord]:
    """Parse Diff_Quality_Estimation JSONL files (cls-*.jsonl)."""
    records: List[CanonicalRecord] = []
    if not dir_path.exists():
        return records

    jsonl_files = sorted(dir_path.glob("cls-valid.jsonl")) + sorted(dir_path.glob("cls-test.jsonl")) + sorted(dir_path.glob("cls-train*.jsonl"))

    for file_path in jsonl_files:
        logger.info(f"Parsing Microsoft Quality_Estimation file: {file_path.name}")
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line_idx, line in enumerate(f):
                if max_records and len(records) >= max_records:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                patch = sanitize_text(data.get("patch", ""))
                msg = sanitize_text(data.get("msg", ""))
                y_label = data.get("y", 0)
                proj = data.get("proj", "unknown")
                lang = _map_language(data.get("lang"))
                # Use file stem + line index to guarantee globally unique ID
                rec_id = f"ms_cls_{file_path.stem}_{line_idx}"

                if not patch:
                    continue

                text_parts = [
                    f"Microsoft Quality Estimation Record ({lang}):",
                    f"Quality Label: {'Acceptable/High Quality' if y_label == 1 else 'Requires Refinement/Issue Found'}",
                    f"Project: {proj}"
                ]
                if msg:
                    text_parts.append(f"Review Comment: {msg}")
                text_parts.append(f"Code Diff Patch:\n{patch}")

                full_text = "\n".join(text_parts)

                rec = CanonicalRecord(
                    id=rec_id,
                    text=full_text,
                    source="microsoft_codereviewer",
                    language=lang,
                    type="quality_estimation",
                    tags=[lang, "quality_estimation"],
                    metadata={
                        "project": proj,
                        "quality_label": y_label,
                        "category": "Diff_Quality_Estimation",
                        "file_source": file_path.name
                    }
                )

                if validate_canonical_record(rec):
                    records.append(rec)

        if max_records and len(records) >= max_records:
            break

    return records


def parse_code_refinement_files(dir_path: Path, max_records: Optional[int] = 300) -> List[CanonicalRecord]:
    """Parse Code_Refinement JSONL files (ref-*.jsonl)."""
    records: List[CanonicalRecord] = []
    if not dir_path.exists():
        return records

    jsonl_files = sorted(dir_path.glob("ref-valid.jsonl")) + sorted(dir_path.glob("ref-test.jsonl")) + sorted(dir_path.glob("ref-train*.jsonl"))

    for file_path in jsonl_files:
        logger.info(f"Parsing Microsoft Code_Refinement file: {file_path.name}")
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for line_idx, line in enumerate(f):
                if max_records and len(records) >= max_records:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue

                comment = sanitize_text(data.get("comment", ""))
                old_code = sanitize_text(data.get("old", ""))
                new_code = sanitize_text(data.get("new", ""))
                repo = data.get("repo", "unknown")
                lang = _map_language(data.get("lang"))
                # Use file stem + line index to guarantee globally unique ID
                rec_id = f"ms_ref_{file_path.stem}_{line_idx}"

                if not comment and not old_code:
                    continue

                text_parts = [
                    f"Microsoft Code Refinement Pair ({lang}):",
                    f"Repository: {repo}"
                ]
                if comment:
                    text_parts.append(f"Review Comment: {comment}")
                if old_code:
                    text_parts.append(f"Before Refinement Code:\n{old_code[:1000]}")
                if new_code:
                    text_parts.append(f"After Refinement Code:\n{new_code[:1000]}")

                full_text = "\n".join(text_parts)

                rec = CanonicalRecord(
                    id=rec_id,
                    text=full_text,
                    source="microsoft_codereviewer",
                    language=lang,
                    type="code_refinement",
                    tags=[lang, "code_refinement"],
                    metadata={
                        "repo": repo,
                        "category": "Code_Refinement",
                        "file_source": file_path.name
                    }
                )

                if validate_canonical_record(rec):
                    records.append(rec)

        if max_records and len(records) >= max_records:
            break

    return records


def load_microsoft_codereviewer(
    base_dir: Path = MS_CODEREVIEWER_DIR,
    max_per_category: int = 500
) -> List[CanonicalRecord]:
    """
    Load and aggregate Microsoft CodeReviewer records across all category subdirectories.
    Prioritizes Comment_Generation and Diff_Quality_Estimation, with Code_Refinement as secondary.
    """
    if not base_dir.exists():
        logger.warning(f"Microsoft CodeReviewer directory not found at: {base_dir}")
        return []

    comment_gen_records = parse_comment_generation_files(
        base_dir / "Comment_Generation", max_records=max_per_category
    )
    quality_est_records = parse_diff_quality_files(
        base_dir / "Diff_Quality_Estimation", max_records=max_per_category
    )
    code_refine_records = parse_code_refinement_files(
        base_dir / "Code_Refinement", max_records=int(max_per_category * 0.6)
    )

    all_ms_records = comment_gen_records + quality_est_records + code_refine_records
    logger.info(f"Loaded {len(all_ms_records)} total canonical records from Microsoft CodeReviewer dataset.")
    return all_ms_records
