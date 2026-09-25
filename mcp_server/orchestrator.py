"""
CRITIQUE ORCHESTRATOR  -- End-to-End GitHub PR Review Pipeline
=============================================================

This is the SINGLE integration point that wires the three standalone modules:

    1. RAG module       -> retrieve_context(code) -> list[str]
    2. LLM module       -> analyze_code(code, context) -> str
    3. GitHub/MCP module  -> fetch PR diff, post review comment

Pipeline flow:
    GitHub PR event  -> Fetch diff  -> RAG retrieval  -> LLM inference
     -> Classify (Easy/Hard)  -> Post comment to PR  -> Log result

This file is the ONLY file that imports from all three modules,
keeping the module boundaries clean.
"""

import os
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_TORCH", "1")

import json
import logging
import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field, asdict

logger = logging.getLogger("critique.orchestrator")


# -------------------------------------------------------------
# DATA CLASSES  -- Pipeline inputs and outputs
# -------------------------------------------------------------

@dataclass
class PipelineResult:
    """Complete record of one pipeline run, for logging and debugging."""
    file_path: str                       # Which file in the PR was reviewed
    code_snippet: str                    # The actual code / diff that was analyzed
    retrieved_context: List[str]         # What the RAG module returned
    llm_output: str                      # Raw text from the fine-tuned model
    classification: str                  # "Easy" or "Hard"
    classification_reason: str           # Why it was classified that way
    comment_posted: bool                 # Whether the GitHub comment was successfully posted
    comment_post_error: Optional[str]    # Error message if posting failed
    timestamp: str = field(default_factory=lambda: datetime.datetime.now().isoformat())


# -------------------------------------------------------------
# CLASSIFICATION  -- Easy vs Hard (keyword/heuristic-based)
# -------------------------------------------------------------

# Keywords that indicate a HARD issue (security, logic, correctness)
HARD_KEYWORDS = [
    "sql injection", "injection", "security", "vulnerability", "vulnerable",
    "crash", "data loss", "data leak", "authentication", "authorization",
    "race condition", "deadlock", "buffer overflow", "memory leak",
    "resource leak", "unclosed", "integer overflow", "off-by-one",
    "logic error", "logic bug", "null pointer", "null reference",
    "divide by zero", "infinite loop", "denial of service", "dos",
    "xss", "cross-site", "csrf", "ssrf", "rce", "remote code execution",
    "path traversal", "directory traversal", "privilege escalation",
    "critical", "high",  # severity keywords from the model output
    "bug",
]

# Keywords that indicate an EASY issue (style, formatting, naming)
EASY_KEYWORDS = [
    "naming", "naming convention", "style", "formatting", "readability",
    "docstring", "comment", "whitespace", "indentation", "pep8", "pep 8",
    "pylint", "flake8", "type hint", "type annotation", "import order",
    "unused import", "unused variable", "magic number", "refactoring",
    "camelcase", "snake_case", "line length", "trailing whitespace",
    "low",  # severity keyword from the model output
]


def classify_review(llm_output: str) -> tuple[str, str]:
    """
    Classify the LLM's review output as "Easy" or "Hard" using
    keyword/heuristic matching.

    Strategy:
      1. Check for HARD keywords first (security, logic, crashes, etc.)
      2. Check for EASY keywords (style, naming, formatting)
      3. Default to "Hard" if uncertain  -- err on the side of caution

    Args:
        llm_output: Raw text output from the fine-tuned Qwen model.

    Returns:
        Tuple of (classification, reason) where classification is
        "Easy" or "Hard" and reason explains which keywords matched.
    """
    if not llm_output or not llm_output.strip():
        return "Hard", "Empty LLM output  -- defaulting to Hard for safety"

    lower = llm_output.lower()

    # --- Check for "Issue Found: No"  -- if no issue, classify as Easy ---
    # The model outputs "Issue Found: Yes/No" in its structured format
    if "issue found: no" in lower or "issue found: false" in lower:
        return "Easy", "No issue found by the model"

    # --- Check HARD keywords ---
    matched_hard = [kw for kw in HARD_KEYWORDS if kw in lower]
    matched_easy = [kw for kw in EASY_KEYWORDS if kw in lower]

    if matched_hard and not matched_easy:
        return "Hard", f"Matched hard keywords: {', '.join(matched_hard[:5])}"

    if matched_easy and not matched_hard:
        return "Easy", f"Matched easy keywords: {', '.join(matched_easy[:5])}"

    if matched_hard and matched_easy:
        # Both matched  -- Hard takes priority (err on side of caution)
        return "Hard", f"Both keyword types matched (hard: {', '.join(matched_hard[:3])}; easy: {', '.join(matched_easy[:3])}). Defaulting to Hard."

    # --- No keywords matched  -- default to Hard ---
    return "Hard", "No classification keywords matched  -- defaulting to Hard for safety"


# -------------------------------------------------------------
# GITHUB COMMENT POSTING
# -------------------------------------------------------------

def post_review_comment_to_pr(
    owner: str,
    repo: str,
    pr_number: int,
    comment_body: str,
    github_token: Optional[str] = None,
) -> bool:
    """
    Post a review comment to a GitHub pull request using the REST API.

    Posts a top-level issue comment (not an inline line comment) so it
    appears in the PR's conversation timeline.

    Args:
        owner: Repository owner (e.g. "ShreyasBasaragi")
        repo: Repository name (e.g. "AI-Code-Reviewer")
        pr_number: Pull request number
        comment_body: Markdown-formatted comment text to post
        github_token: GitHub personal access token (falls back to env var)

    Returns:
        True if comment was posted successfully, False otherwise.

    Raises:
        RuntimeError: If posting fails due to auth or network issues.
    """
    import urllib.request
    import urllib.error

    token = github_token or os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN not set. Provide a token via argument or "
            "set the GITHUB_TOKEN environment variable."
        )

    # GitHub Issues API  -- PRs are a superset of issues, so we post
    # to the issues endpoint which creates a conversation comment
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"

    payload = json.dumps({"body": comment_body}).encode("utf-8")

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "Critique-AI-Code-Reviewer",
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as response:
            if response.status in (200, 201):
                logger.info(f"Successfully posted review comment to PR #{pr_number}")
                return True
            else:
                logger.warning(f"Unexpected HTTP status {response.status} posting comment")
                return False
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace") if err.fp else ""
        raise RuntimeError(
            f"GitHub API error {err.code} posting comment to "
            f"{owner}/{repo}#{pr_number}: {err.reason}\n{body}"
        ) from err
    except urllib.error.URLError as err:
        raise RuntimeError(f"Network error posting comment: {err.reason}") from err


def format_review_comment(
    file_path: str,
    classification: str,
    classification_reason: str,
    llm_output: str,
) -> str:
    """
    Format the final GitHub PR comment in clean Markdown.

    Args:
        file_path: The file being reviewed
        classification: "Easy" or "Hard"
        classification_reason: Why it was classified that way
        llm_output: Raw review text from the fine-tuned model

    Returns:
        Markdown-formatted comment string ready to post to GitHub.
    """
    # Choose emoji based on classification
    badge = "[Easy]" if classification == "Easy" else "[Hard]"

    comment = (
        f"## Critique -- AI Code Review\n\n"
        f"**File:** `{file_path}`\n"
        f"**Classification:** {badge}\n"
        f"**Reason:** {classification_reason}\n\n"
        f"---\n\n"
        f"### Review\n\n"
        f"{llm_output}\n\n"
        f"---\n"
        f"*Generated by [Critique](https://github.com/ShreyasBasaragi/AI-Code-Reviewer) -- "
        f"RAG-augmented fine-tuned Qwen 2.5 Coder 3B*"
    )
    return comment


# -------------------------------------------------------------
# CORE PIPELINE  -- The single orchestration function
# -------------------------------------------------------------

# Lazy-loaded LLM instance (shared across all pipeline calls)
_llm_instance = None


def _get_llm():
    """Lazy-initialize the fine-tuned Qwen LLM. Shared singleton."""
    global _llm_instance
    if _llm_instance is None:
        from llm.qwen_client import QwenLLM
        logger.info("Initializing fine-tuned Qwen 2.5 Coder 3B LoRA model...")
        _llm_instance = QwenLLM()
        logger.info("LLM initialized successfully.")
    return _llm_instance


def run_review_pipeline(
    code: str,
    file_path: str = "unknown_file.py",
    owner: str = "",
    repo: str = "",
    pr_number: int = 0,
    post_to_github: bool = False,
    github_token: Optional[str] = None,
    notify_desktop: bool = True,
) -> PipelineResult:
    """
    Run the complete Critique review pipeline on a single code snippet.

    This is the core orchestration function that calls all three modules
    in sequence: RAG  -> LLM  -> Classification  -> (optionally) GitHub post.

    Pipeline steps:
      1. Call retrieve_context(code) from the RAG module
      2. Call analyze_code(code, context) from the LLM module
      3. Classify the LLM output as Easy or Hard
      4. Optionally post a formatted comment to the GitHub PR
      5. Optionally trigger a Windows desktop toast notification
      6. Return a PipelineResult with full traceability

    Error handling:
      - If RAG fails  -> proceed with empty context (LLM still runs)
      - If LLM fails  -> post a fallback "analysis unavailable" comment
      - If GitHub post fails  -> record the error but don't crash

    Args:
        code: Source code string or unified diff to review.
        file_path: Path of the file being reviewed (for display).
        owner: GitHub repo owner (required if post_to_github=True).
        repo: GitHub repo name (required if post_to_github=True).
        pr_number: PR number (required if post_to_github=True).
        post_to_github: Whether to actually post the comment to GitHub.
        github_token: GitHub token (falls back to GITHUB_TOKEN env var).
        notify_desktop: Whether to fire a Windows desktop toast alert.

    Returns:
        PipelineResult dataclass with full pipeline trace for logging.
    """
    logger.info(f"=== Pipeline start: {file_path} ===")

    # -- STEP 1: RAG Context Retrieval ------------------------
    retrieved_context: List[str] = []
    try:
        from rag.retrieve import retrieve_context
        logger.info("[Step 1/5] Retrieving RAG context from ChromaDB...")
        retrieved_context = retrieve_context(code, k=5)
        logger.info(f"  -> Retrieved {len(retrieved_context)} context snippets.")
    except Exception as e:
        # RAG failure is non-fatal  -- proceed with empty context
        logger.warning(f"   -> RAG retrieval failed: {e}. Proceeding with empty context.")
        retrieved_context = []

    # -- STEP 2: LLM Inference --------------------------------
    llm_output = ""
    llm_failed = False
    try:
        logger.info("[Step 2/5] Running fine-tuned Qwen 2.5 Coder LLM inference...")
        llm = _get_llm()
        llm_output = llm.analyze_code(code=code, context=retrieved_context)
        logger.info(f"   -> LLM generated {len(llm_output)} chars of review text.")
    except Exception as e:
        # LLM failure  -- we'll post a fallback comment
        logger.error(f"   -> LLM inference failed: {e}")
        llm_output = (
            f"[WARNING] **Critique was unable to complete automated analysis for `{file_path}`.**\n\n"
            f"Error: `{str(e)[:200]}`\n\n"
            f"Please review this file manually."
        )
        llm_failed = True

    # -- STEP 3: Classify as Easy or Hard ---------------------
    if llm_failed:
        classification = "Hard"
        classification_reason = "LLM inference failed  -- defaulting to Hard for manual review"
    else:
        logger.info("[Step 3/5] Classifying review as Easy or Hard...")
        classification, classification_reason = classify_review(llm_output)
        logger.info(f"   -> Classification: {classification} ({classification_reason})")

    # -- STEP 4: Post comment to GitHub PR --------------------
    comment_posted = False
    comment_post_error = None

    comment_body = format_review_comment(
        file_path=file_path,
        classification=classification,
        classification_reason=classification_reason,
        llm_output=llm_output,
    )

    if post_to_github and owner and repo and pr_number:
        try:
            logger.info(f"[Step 4/5] Posting review comment to {owner}/{repo}#{pr_number}...")
            comment_posted = post_review_comment_to_pr(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                comment_body=comment_body,
                github_token=github_token,
            )
            logger.info(f"   -> Comment posted successfully: {comment_posted}")
        except Exception as e:
            comment_post_error = str(e)
            logger.error(f"   -> Failed to post comment: {e}")
    else:
        logger.info("[Step 4/5] Skipping GitHub post (post_to_github=False or missing params).")

    # -- STEP 5: Desktop notification -------------------------
    if notify_desktop:
        try:
            from mcp_server.notifier import notify_user_if_issue
            logger.info("[Step 5/5] Checking if desktop notification should fire...")
            notified = notify_user_if_issue(llm_output, file_path=file_path)
            if notified:
                logger.info("   -> Desktop toast notification dispatched.")
            else:
                logger.info("   -> No critical issues  -- toast suppressed.")
        except Exception as e:
            logger.warning(f"   -> Desktop notification failed: {e}")
    else:
        logger.info("[Step 5/5] Desktop notifications disabled.")

    # -- Build and return the pipeline result ------------------
    result = PipelineResult(
        file_path=file_path,
        code_snippet=code[:500] if len(code) > 500 else code,  # Truncate for logging
        retrieved_context=[c[:200] for c in retrieved_context],  # Truncate for logging
        llm_output=llm_output,
        classification=classification,
        classification_reason=classification_reason,
        comment_posted=comment_posted,
        comment_post_error=comment_post_error,
    )

    logger.info(f"=== Pipeline complete: {file_path} -> {classification} ===\n")
    return result


# -------------------------------------------------------------
# PR EVENT HANDLER  -- Entry point for GitHub webhook events
# -------------------------------------------------------------

def handle_pr_event(
    pr_data: Dict[str, Any],
    github_token: Optional[str] = None,
    notify_desktop: bool = True,
) -> List[PipelineResult]:
    """
    Handle a GitHub pull request event end-to-end.

    This is the top-level entry point that would be called by a
    webhook listener. It:
      1. Extracts owner/repo/pr_number from the PR event payload
      2. Fetches the list of changed files and their diffs
      3. Runs the full review pipeline on each changed file
      4. Posts a comment for each file back to the PR
      5. Returns all PipelineResult objects for logging

    Args:
        pr_data: GitHub webhook PR event payload (or a mock dict with
                 keys: owner, repo, pr_number, and optionally files).
        github_token: GitHub PAT. Falls back to GITHUB_TOKEN env var.
        notify_desktop: Whether to fire Windows toast alerts.

    Returns:
        List of PipelineResult objects, one per reviewed file.
    """
    # Extract PR metadata from the event payload
    owner = pr_data.get("owner", "")
    repo = pr_data.get("repo", "")
    pr_number = pr_data.get("pr_number", 0)
    post_to_github = pr_data.get("post_to_github", False)

    logger.info(f"+----------------------------------------------+")
    logger.info(f"|  Handling PR event: {owner}/{repo}#{pr_number}")
    logger.info(f"+----------------------------------------------+")

    # --- Fetch changed files from GitHub if not provided ---
    files = pr_data.get("files", None)

    if files is None and owner and repo and pr_number:
        try:
            from rag.github.client import GitHubClient
            client = GitHubClient(token=github_token)
            logger.info(f"Fetching changed files from GitHub API for PR #{pr_number}...")
            raw_files = client.get_pr_files(owner, repo, pr_number)
            files = []
            for f in raw_files:
                files.append({
                    "filename": f.get("filename", "unknown"),
                    "patch": f.get("patch", ""),
                    "status": f.get("status", "modified"),
                })
            logger.info(f"Fetched {len(files)} changed files from PR #{pr_number}.")
        except Exception as e:
            logger.error(f"Failed to fetch PR files from GitHub: {e}")
            return []

    if not files:
        logger.warning("No files to review. Aborting pipeline.")
        return []

    # --- Run pipeline on each changed file ---
    results: List[PipelineResult] = []

    for file_info in files:
        filename = file_info.get("filename", "unknown")
        patch = file_info.get("patch", "")

        # Skip non-code files (images, configs, lockfiles, etc.)
        if not patch or not patch.strip():
            logger.info(f"Skipping {filename}  -- no patch/diff content.")
            continue

        code_extensions = {".py", ".js", ".ts", ".java", ".go", ".rs", ".cpp", ".c", ".rb"}
        ext = Path(filename).suffix.lower()
        if ext not in code_extensions:
            logger.info(f"Skipping {filename}  -- not a supported code file ({ext}).")
            continue

        result = run_review_pipeline(
            code=patch,
            file_path=filename,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            post_to_github=post_to_github,
            github_token=github_token,
            notify_desktop=notify_desktop,
        )
        results.append(result)

    # --- Log summary ---
    logger.info(f"PR #{pr_number} review complete: {len(results)} files reviewed.")
    for r in results:
        status = "[OK] Posted" if r.comment_posted else "[*] Local only"
        logger.info(f"  {r.file_path}: {r.classification} [{status}]")

    return results


# -------------------------------------------------------------
# PIPELINE RESULT LOGGING  -- Save results to JSON for debugging
# -------------------------------------------------------------

LOG_DIR = Path(__file__).resolve().parent / "logs"


def save_pipeline_results(results: List[PipelineResult], label: str = "run") -> Path:
    """
    Save a list of PipelineResult objects to a timestamped JSON log file.

    Args:
        results: List of PipelineResult dataclass instances.
        label: A short label for the log file name (e.g. "test", "pr-42").

    Returns:
        Path to the saved JSON log file.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = LOG_DIR / f"pipeline_{label}_{timestamp}.json"

    log_data = {
        "label": label,
        "timestamp": timestamp,
        "total_files_reviewed": len(results),
        "results": [asdict(r) for r in results],
    }

    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, ensure_ascii=False)

    logger.info(f"Pipeline results saved to: {log_file}")
    return log_file
