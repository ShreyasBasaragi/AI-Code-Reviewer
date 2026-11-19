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
import time
from pathlib import Path
from typing import Callable, List, Dict, Any, Optional, Sequence, Union
from dataclasses import dataclass, field, asdict, replace

from llm.base import BaseLLM
from fix_generation import FixCandidate, failed_fix_candidate, parse_fix_response
from fix_verification import FixVerificationResult, verify_fix_candidate as _verify_fix_candidate
from fix_lifecycle import (
    FixApprovalResult,
    FixExportResult,
    accept_fix_candidate as _accept_fix_candidate,
    export_fix_candidate as _export_fix_candidate,
    reject_fix_candidate as _reject_fix_candidate,
)
from review_schema import RetrievedEvidence, ReviewResponse, parse_review_response
from static_analysis import StaticAnalysisResult, analyze_python

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
    review_response: ReviewResponse      # Validated structured response and raw output
    retrieved_evidence: List[RetrievedEvidence]
    classification: str                  # "Easy" or "Hard"
    classification_reason: str           # Why it was classified that way
    comment_posted: bool                 # Whether the GitHub comment was successfully posted
    comment_post_error: Optional[str]    # Error message if posting failed
    timestamp: str = field(default_factory=lambda: datetime.datetime.now().isoformat())
    phase_timings: Dict[str, float] = field(default_factory=dict)


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
_fix_llm_instance = None
_llm_settings_generation = None
_fix_llm_settings_generation = None


def _get_llm():
    """Lazy-initialize the configured review provider once per process."""
    global _llm_instance, _llm_settings_generation
    from llm.provider_factory import get_configured_provider
    from llm.provider_settings import PROVIDER_SETTINGS

    generation = PROVIDER_SETTINGS.public_snapshot()["generation"]
    if _llm_instance is None or generation != _llm_settings_generation:
        _llm_instance = get_configured_provider("review")
        _llm_settings_generation = generation
        logger.info("Configured review provider initialized.")
    return _llm_instance


def _get_fix_llm():
    """Use the separately configured fix provider; never switch on failure."""
    global _fix_llm_instance, _fix_llm_settings_generation
    from llm.provider_factory import get_configured_provider
    from llm.provider_settings import PROVIDER_SETTINGS

    generation = PROVIDER_SETTINGS.public_snapshot()["generation"]
    if _fix_llm_instance is None or generation != _fix_llm_settings_generation:
        _fix_llm_instance = get_configured_provider("fix")
        _fix_llm_settings_generation = generation
        logger.info("Configured fix-generation provider initialized.")
    return _fix_llm_instance


def run_review_pipeline(
    code: str,
    file_path: str = "unknown_file.py",
    owner: str = "",
    repo: str = "",
    pr_number: int = 0,
    post_to_github: bool = False,
    github_token: Optional[str] = None,
    notify_desktop: bool = True,
    llm: Optional[BaseLLM] = None,
    retriever: Optional[Callable[[str], List[Union[str, RetrievedEvidence]]]] = None,
    supplemental_evidence: Optional[Sequence[RetrievedEvidence]] = None,
    phase_timings: Optional[Dict[str, float]] = None,
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
    pipeline_started = time.perf_counter()
    phase_timings = phase_timings if phase_timings is not None else {}
    logger.info(f"=== Pipeline start: {file_path} ===")

    # -- STEP 1: RAG Context Retrieval ------------------------
    retrieved_context: List[str] = []
    retrieved_evidence: List[RetrievedEvidence] = []
    retrieval_error: Optional[str] = None
    retrieval_started = time.perf_counter()
    try:
        logger.info("[Step 1/5] Retrieving RAG context from ChromaDB...")
        if retriever is None:
            from rag.retrieve import retrieve_evidence_with_diagnostics

            def retriever(source: str):
                evidence, diagnostics = retrieve_evidence_with_diagnostics(source)
                if diagnostics.get("status") == "errored":
                    from rag.retrieve_v3 import RAGConfigurationError

                    error_type = diagnostics.get("error_type", "unknown")
                    raise RAGConfigurationError(
                        f"configured RAG corpus could not be queried ({error_type})"
                    )
                return evidence
        retrieved = retriever(code)
        for item in retrieved or []:
            if isinstance(item, RetrievedEvidence):
                retrieved_evidence.append(item)
                retrieved_context.append(item.to_prompt_text())
            elif isinstance(item, str):
                # Keep existing injected/list[str] retrievers working. These legacy
                # strings have no stable IDs and therefore cannot be cited.
                retrieved_context.append(item)
            else:
                raise TypeError("retriever must return RetrievedEvidence items or strings")
        logger.info(f"  -> Retrieved {len(retrieved_context)} context snippets.")
    except Exception as e:
        # RAG failure is non-fatal  -- proceed with empty context
        logger.warning(
            "   -> RAG retrieval failed (%s). Proceeding with empty context.",
            type(e).__name__,
        )
        retrieved_context = []
        retrieval_error = type(e).__name__
    evidence_ids = {item.evidence_id for item in retrieved_evidence if item.evidence_id}
    supplemental_error: Optional[str] = None
    for item in supplemental_evidence or ():
        if not isinstance(item, RetrievedEvidence) or not item.evidence_id:
            supplemental_error = "supplemental_evidence_invalid"
            continue
        if item.evidence_id in evidence_ids:
            supplemental_error = "supplemental_evidence_id_collision"
            continue
        evidence_ids.add(item.evidence_id)
        retrieved_evidence.append(item)
        retrieved_context.append(item.to_prompt_text())
    logger.info("  -> Total evidence snippets: %d", len(retrieved_context))
    phase_timings["rag_retrieval_seconds"] = time.perf_counter() - retrieval_started

    # -- STEP 2: LLM Inference --------------------------------
    llm_output = ""
    llm_failed = False
    model_error: Optional[str] = None
    inference_started = time.perf_counter()
    try:
        logger.info("[Step 2/5] Running the configured LLM provider...")
        if llm is None:
            llm = _get_llm()
        llm_output = llm.analyze_code(code=code, context=retrieved_context)
        logger.info(f"   -> LLM generated {len(llm_output)} chars of review text.")
    except Exception as e:
        # LLM failure  -- we'll post a fallback comment
        logger.error("   -> LLM inference failed (%s).", type(e).__name__)
        llm_output = (
            f"[WARNING] **Critique was unable to complete automated analysis for `{file_path}`.**\n\n"
            f"Provider error type: `{type(e).__name__}`.\n\n"
            f"Please review this file manually."
        )
        llm_failed = True
        model_error = type(e).__name__
    phase_timings["model_generation_seconds"] = time.perf_counter() - inference_started

    validation_started = time.perf_counter()
    review_response = parse_review_response(llm_output, retrieved_evidence)
    phase_timings["response_validation_seconds"] = time.perf_counter() - validation_started
    static_started = time.perf_counter()
    static_result = analyze_python(code, file_path=file_path)
    phase_timings["static_analysis_seconds"] = time.perf_counter() - static_started
    static_findings = tuple(_static_review_finding(item) for item in static_result.findings)
    review_response = ReviewResponse(
        summary=review_response.summary,
        findings=(
            _merge_review_findings(review_response.findings, static_findings)
            if review_response.valid else ()
        ),
        processing_errors=review_response.processing_errors,
        raw_response=review_response.raw_response,
        valid=review_response.valid,
        evidence=review_response.evidence,
        static_analysis_status=static_result.status,
        static_analysis_error=_static_error_payload(static_result),
    )
    phase_timings["validation_seconds"] = phase_timings["response_validation_seconds"]
    processing_errors = list(review_response.processing_errors)
    if retrieval_error:
        processing_errors.append(f"retrieval_failure: {retrieval_error}")
    if supplemental_error:
        processing_errors.append(supplemental_error)
    if model_error:
        processing_errors.append(f"model_failure: {model_error}")
    if static_result.status == "errored":
        processing_errors.append("static_analysis_parse_error")

    if processing_errors != list(review_response.processing_errors) or model_error:
        review_response = ReviewResponse(
            summary=review_response.summary,
            findings=review_response.findings,
            processing_errors=tuple(processing_errors),
            raw_response=llm_output,
            valid=review_response.valid and not model_error,
            evidence=review_response.evidence,
            static_analysis_status=static_result.status,
            static_analysis_error=_static_error_payload(static_result),
        )

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

    if post_to_github and owner and repo and pr_number and not review_response.valid:
        comment_post_error = "Skipped GitHub posting because the model response failed validation."
        logger.warning(comment_post_error)
    elif post_to_github and owner and repo and pr_number:
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
    if notify_desktop and review_response.valid:
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
    phase_timings["total_review_seconds"] = time.perf_counter() - pipeline_started
    result = PipelineResult(
        file_path=file_path,
        code_snippet=code[:500] if len(code) > 500 else code,  # Truncate for logging
        retrieved_context=[c[:200] for c in retrieved_context],  # Truncate for logging
        llm_output=llm_output,
        review_response=review_response,
        retrieved_evidence=retrieved_evidence,
        classification=classification,
        classification_reason=classification_reason,
        comment_posted=comment_posted,
        comment_post_error=comment_post_error,
        phase_timings=dict(phase_timings),
    )

    logger.info(f"=== Pipeline complete: {file_path} -> {classification} ===\n")
    return result


def generate_fix_candidate(
    review_response: ReviewResponse,
    finding_ref: str,
    original_code: str,
    language: str = "python",
    llm: Optional[BaseLLM] = None,
) -> FixCandidate:
    """Generate one unapplied, unverified fix for a selected validated finding."""
    safe_finding_ref = finding_ref if isinstance(finding_ref, str) else ""
    safe_original_code = original_code if isinstance(original_code, str) else ""
    if not isinstance(review_response, ReviewResponse) or not review_response.valid:
        return failed_fix_candidate(
            finding_ref=safe_finding_ref,
            original_code=safe_original_code,
            error="invalid_review_response: a valid structured review is required",
        )
    reparsed = parse_review_response(review_response.raw_response, review_response.evidence)
    llm_findings = tuple(
        replace(item, rule_id=None, severity=None, also_detected_by=())
        for item in review_response.findings if item.origin == "llm"
    )
    static_findings = tuple(item for item in review_response.findings if item.origin == "static")
    static_file_path = static_findings[0].affected_file if static_findings else "submitted.py"
    static_result = analyze_python(safe_original_code, file_path=static_file_path or "submitted.py")
    expected_static = tuple(_static_review_finding(item) for item in static_result.findings)
    expected_static_keys = sorted((item.rule_id, item.line_start, item.line_end) for item in expected_static)
    represented_static_keys = sorted(
        [(item.rule_id, item.line_start, item.line_end) for item in static_findings]
        + [
            (item.rule_id, item.line_start, item.line_end)
            for item in review_response.findings
            if item.origin == "llm" and "static" in item.also_detected_by and item.rule_id
        ]
    )
    if (
        not reparsed.valid
        or reparsed.summary != review_response.summary
        or reparsed.findings != llm_findings
        or expected_static_keys != represented_static_keys
        or any(item not in expected_static for item in static_findings)
    ):
        return failed_fix_candidate(
            finding_ref=safe_finding_ref,
            original_code=safe_original_code,
            error="invalid_review_response: response_does_not_match_validated_raw_output",
        )
    if not isinstance(language, str) or language.casefold() != "python":
        return failed_fix_candidate(
            finding_ref=safe_finding_ref,
            original_code=safe_original_code,
            error="unsupported_language: only Python fix generation is available",
        )
    if not safe_original_code.strip():
        return failed_fix_candidate(
            finding_ref=safe_finding_ref,
            original_code=safe_original_code,
            error="invalid_original_code: expected_nonempty_python_source",
        )

    finding = next(
        (item for item in review_response.findings if item.finding_id == safe_finding_ref),
        None,
    )
    if finding is None:
        return failed_fix_candidate(
            finding_ref=safe_finding_ref,
            original_code=safe_original_code,
            error="finding_not_found: selected_finding_does_not_exist",
        )
    if finding.abstained:
        return failed_fix_candidate(
            finding_ref=safe_finding_ref,
            original_code=safe_original_code,
            error="finding_abstained: cannot generate a fix for an abstained finding",
        )

    evidence_by_id = {
        item.evidence_id: item
        for item in review_response.evidence
        if item.evidence_id
    }
    missing_ids = [item for item in finding.evidence_ids if item not in evidence_by_id]
    if missing_ids:
        return failed_fix_candidate(
            finding_ref=safe_finding_ref,
            original_code=safe_original_code,
            error="invalid_review_response: finding_references_missing_evidence",
        )
    evidence = [evidence_by_id[item] for item in finding.evidence_ids]
    context = [item.to_prompt_text() for item in evidence]

    try:
        provider = llm if llm is not None else _get_fix_llm()
        generate = getattr(provider, "generate_fix", None)
        if not callable(generate):
            raise NotImplementedError("LLM provider does not support fix generation")
        raw_response = generate(safe_original_code, finding, context)
    except Exception as exc:
        return failed_fix_candidate(
            finding_ref=safe_finding_ref,
            original_code=safe_original_code,
            error=f"fix_generation_failed: {type(exc).__name__}",
        )

    return parse_fix_response(
        raw_response,
        finding_ref=safe_finding_ref,
        original_code=safe_original_code,
        allowed_evidence_ids=finding.evidence_ids,
    )


def _static_review_finding(item) -> "ReviewFinding":
    from review_schema import ReviewFinding

    return ReviewFinding(
        title=item.title,
        issue_type="security",
        explanation=item.explanation,
        recommended_fix="Replace this construct with a safer alternative and validate any external input.",
        affected_file=item.affected_file,
        line_start=item.line_start,
        line_end=item.line_end,
        evidence_ids=(),
        uncertain=False,
        abstained=False,
        risk_priority=None,
        finding_id=item.finding_id,
        origin="static",
        rule_id=item.rule_id,
        severity=item.severity,
    )


def _static_error_payload(result: StaticAnalysisResult) -> Optional[dict[str, Any]]:
    if result.status != "errored":
        return None
    return {
        "error_type": result.error_type,
        "message": result.error_message,
        "line": result.error_line,
        "column": result.error_column,
    }


def _merge_review_findings(llm_findings, static_findings):
    """Deduplicate exact repeats and uniquely matched same-line rule findings."""
    llm_unique = []
    llm_seen = set()
    for item in llm_findings:
        key = (item.line_start, item.line_end, item.title.casefold(), item.explanation.casefold())
        if key not in llm_seen:
            llm_seen.add(key)
            llm_unique.append(item)

    static_unique = []
    static_seen = set()
    for item in static_findings:
        key = (item.rule_id, item.line_start, item.line_end, item.finding_id)
        if key not in static_seen:
            static_seen.add(key)
            static_unique.append(item)

    llm_matches: dict[int, list[int]] = {}
    static_matches: dict[int, list[int]] = {}
    for llm_index, llm_item in enumerate(llm_unique):
        for static_index, static_item in enumerate(static_unique):
            if _same_static_issue(llm_item, static_item):
                llm_matches.setdefault(llm_index, []).append(static_index)
                static_matches.setdefault(static_index, []).append(llm_index)

    paired_static: set[int] = set()
    for llm_index, static_indexes in llm_matches.items():
        if len(static_indexes) != 1:
            continue
        static_index = static_indexes[0]
        if len(static_matches.get(static_index, ())) != 1:
            continue
        static_item = static_unique[static_index]
        llm_unique[llm_index] = replace(
            llm_unique[llm_index], rule_id=static_item.rule_id,
            severity=static_item.severity, also_detected_by=("static",),
        )
        paired_static.add(static_index)

    merged = list(llm_unique)
    merged.extend(item for index, item in enumerate(static_unique) if index not in paired_static)
    return tuple(merged)


def _same_static_issue(llm_finding, static_finding) -> bool:
    if llm_finding.line_start != static_finding.line_start:
        return False
    if llm_finding.line_end not in (None, static_finding.line_end):
        return False
    if llm_finding.affected_file and llm_finding.affected_file != static_finding.affected_file:
        return False
    text = (llm_finding.title + " " + llm_finding.explanation).casefold()
    cues = {
        "PYSEC001": ("eval", "exec", "dynamic code"),
        "PYSEC002": ("pickle", "dill", "deserializ"),
        "PYSEC003": ("yaml.load", "unsafe yaml", "yaml loader"),
        "PYSEC004": ("shell=true", "shell=True"),
        "PYSEC005": ("os.system",),
    }
    return any(cue.casefold() in text for cue in cues.get(static_finding.rule_id or "", ()))


def verify_fix_candidate(
    candidate: FixCandidate, *, expected_candidate_id: Optional[str] = None
) -> FixVerificationResult:
    """Verify an existing candidate without model calls, retrieval, or file access."""
    return _verify_fix_candidate(candidate, expected_candidate_id=expected_candidate_id)


def accept_fix(candidate: FixCandidate, *, actor_id: str = "unattributed") -> FixApprovalResult:
    """Accept only a candidate with a matching successful syntax report."""
    return _accept_fix_candidate(candidate, actor_id=actor_id)


def reject_fix(candidate: FixCandidate, *, actor_id: str = "unattributed") -> FixApprovalResult:
    """Reject a candidate without changing its source or review."""
    return _reject_fix_candidate(candidate, actor_id=actor_id)


def export_fix(candidate: FixCandidate) -> FixExportResult:
    """Return accepted corrected code and diff without writing to disk."""
    return _export_fix_candidate(candidate)


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
