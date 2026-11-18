import os
import sys
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from mcp_server.orchestrator import (
    accept_fix as accept_fix_candidate,
    export_fix as export_fix_candidate,
    generate_fix_candidate,
    reject_fix as reject_fix_candidate,
    run_review_pipeline,
    verify_fix_candidate,
)
from fix_generation import failed_fix_candidate
from fix_verification import FixVerificationResult, SyntaxCheckResult
from fix_lifecycle import record_verification
from mcp_server.state import PROCESS_STATE
from mcp_server.github_webhook import WebhookDeliveryStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("critique-mcp")

# Process-local handles let an explicit follow-up MCP call use the exact reviewed
# code and evidence without writing review payloads to disk. Keep only recent items.
_REVIEW_CACHE = PROCESS_STATE.reviews
_REVIEW_CACHE_LIMIT = PROCESS_STATE.limit
_FIX_CANDIDATE_CACHE = PROCESS_STATE.candidates
_FIX_CANDIDATE_CACHE_LIMIT = PROCESS_STATE.limit

# Initialize FastMCP Server
mcp = FastMCP(
    name="Critique AI Code Reviewer",
    instructions="Autonomous AI Code Review Server with RAG Knowledge Retrieval and Desktop Alerts"
)

@mcp.tool()
def review_code(code: str, file_path: str = "", repository: str = "") -> str:
    """
    Review a code snippet or function using the RAG knowledge base and fine-tuned Qwen model.
    Pops up a Windows desktop notification alert if an issue is detected.

    Args:
        code: Source code string to review.
        file_path: Optional file path or module name for context.
        repository: Retained for caller compatibility; repository-filtered retrieval is not implemented.

    Returns:
        JSON review response containing validated findings and source evidence references.
    """
    if not code or not code.strip():
        return "Error: Code snippet is empty."

    logger.info(f"Received review request for: {file_path or 'unnamed snippet'}")

    # Keep MCP requests on the same injectable pipeline used by the PR orchestrator.
    # The repository argument remains accepted for compatibility; retrieval currently
    # uses the existing code-only interface and does not filter by repository.
    result = run_review_pipeline(
        code=code,
        file_path=file_path or "unknown_file.py",
    )
    payload = result.review_response.to_dict()
    if result.review_response.valid:
        review_id = uuid.uuid4().hex
        PROCESS_STATE.put_review(review_id, code, result.review_response)
        payload["review_id"] = review_id
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
def review_file(file_path: str) -> str:
    """
    Read a local source code file from disk, review it, and notify the developer of issues.

    Args:
        file_path: Absolute or relative path to the local file to review.
    """
    path = Path(file_path)
    if not path.exists():
        return f"Error: File not found at path '{file_path}'"

    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return f"Error reading file '{file_path}': {e}"

    return review_code(code=content, file_path=str(path.name))


@mcp.tool()
def review_diff(diff: str, repository: str = "") -> str:
    """
    Review a git patch or pull request diff using RAG historical reviews and fine-tuned Qwen.
    Pops up a desktop alert if issues are identified.

    Args:
        diff: The git unified diff string.
        repository: Optional repository name for historical context matching.
    """
    return review_code(code=diff, file_path="PR Diff", repository=repository)


@mcp.tool()
def generate_fix(review_id: str, finding_ref: str, language: str = "python") -> str:
    """Generate an unapplied, unverified Python fix for one finding from a prior review."""
    handle = PROCESS_STATE.get_review(review_id)
    if handle is None:
        candidate = failed_fix_candidate(
            finding_ref=finding_ref,
            original_code="",
            error="review_not_found: request_a_valid_review_in_this_server_session",
        )
    else:
        _REVIEW_CACHE.move_to_end(review_id)
        original_code, review_response = handle.code, handle.response
        candidate = generate_fix_candidate(
            review_response=review_response,
            finding_ref=finding_ref,
            original_code=original_code,
            language=language,
        )
        if candidate.generation_status == "generated":
            PROCESS_STATE.put_candidate(candidate)
    return json.dumps(candidate.to_dict(), ensure_ascii=False)


@mcp.tool()
def verify_fix(candidate_id: str) -> str:
    """Explicitly diff and statically verify a generated fix; never runs its code."""
    valid_handle = isinstance(candidate_id, str) and bool(candidate_id)
    candidate = PROCESS_STATE.get_candidate(candidate_id) if valid_handle else None
    if candidate is None:
        report = FixVerificationResult(
            candidate_id=candidate_id if valid_handle else "",
            unified_diff="",
            syntax_check=SyntaxCheckResult(check_name="ast.parse", status="not_run"),
            verification_status="verification_error",
            static_analysis_status="errored",
            static_analysis_errors=({"message": "candidate_not_found: handle_unknown_or_expired"},),
            verification_errors=("candidate_not_found: handle_unknown_or_expired",),
        )
        return json.dumps(report.to_dict(), ensure_ascii=False)
    report = verify_fix_candidate(candidate, expected_candidate_id=candidate_id)
    PROCESS_STATE.put_candidate(record_verification(candidate, report))
    return json.dumps(report.to_dict(), ensure_ascii=False)


@mcp.tool()
def accept_fix(candidate_id: str) -> str:
    """Explicitly accept a syntax- and statically verified candidate."""
    candidate = _get_cached_candidate(candidate_id)
    if candidate is None:
        return json.dumps(_candidate_handle_error(candidate_id, "accept"), ensure_ascii=False)
    result = accept_fix_candidate(candidate)
    PROCESS_STATE.put_candidate(result.candidate)
    return json.dumps(result.to_dict(), ensure_ascii=False)


@mcp.tool()
def reject_fix(candidate_id: str) -> str:
    """Explicitly reject a candidate without modifying its source or review."""
    candidate = _get_cached_candidate(candidate_id)
    if candidate is None:
        return json.dumps(_candidate_handle_error(candidate_id, "reject"), ensure_ascii=False)
    result = reject_fix_candidate(candidate)
    PROCESS_STATE.put_candidate(result.candidate)
    return json.dumps(result.to_dict(), ensure_ascii=False)


@mcp.tool()
def export_fix(candidate_id: str) -> str:
    """Return accepted corrected code and diff as JSON; does not write a file."""
    candidate = _get_cached_candidate(candidate_id)
    if candidate is None:
        return json.dumps(_candidate_handle_error(candidate_id, "export"), ensure_ascii=False)
    try:
        payload = export_fix_candidate(candidate).to_dict()
    except ValueError as exc:
        payload = {
            "candidate_id": candidate_id,
            "error": "export_not_allowed",
            "message": str(exc),
            "approval_status": candidate.approval_status,
            "syntax_verification_status": candidate.verification_status,
        }
    return json.dumps(payload, ensure_ascii=False)


def _get_cached_candidate(candidate_id):
    if not isinstance(candidate_id, str) or not candidate_id:
        return None
    return PROCESS_STATE.get_candidate(candidate_id)


def _candidate_handle_error(candidate_id, action):
    return {
        "candidate_id": candidate_id if isinstance(candidate_id, str) else "",
        "action": action,
        "approval_status": "unknown",
        "outcome": "error",
        "error": "candidate_not_found: handle_unknown_or_expired",
    }


@mcp.resource("critique://status")
def get_system_status() -> str:
    """Check Critique server status, active model, and RAG vector store breakdown."""
    from rag.ingest_github import get_chromadb_source_breakdown
    breakdown = get_chromadb_source_breakdown()

    status_lines = [
        "Critique Autonomous AI Code Reviewer Status:",
        f"Active Model: Qwen 2.5 Coder 3B (LoRA Adapter)",
        f"Desktop Notifier: Windows Native Toast (Active)",
        "ChromaDB Indexed Collections:"
    ]
    if breakdown:
        for src, count in breakdown.items():
            status_lines.append(f"  - {src}: {count} records")
    else:
        status_lines.append("  (ChromaDB is empty or uninitialized)")

    return "\n".join(status_lines)


@mcp.tool()
def get_github_action_records(delivery_id: str, status_token: str) -> str:
    """Read sanitized, durable action decisions for one webhook delivery.

    This is a read-only MCP tool. GitHub writes are performed only by the
    policy-gated webhook worker; there are no model-callable write tools.
    """
    from mcp_server.github_webhook import verify_status_token, WebhookError

    try:
        authorized = verify_status_token(
            "Bearer " + status_token if isinstance(status_token, str) else None,
            os.getenv("GITHUB_WEBHOOK_STATUS_TOKEN"),
        )
    except WebhookError:
        authorized = False
    if not authorized:
        return json.dumps({"error": "webhook_status_unauthorized"})
    store = WebhookDeliveryStore.from_environment()
    store.initialize()
    records = store.get_actions(delivery_id) if isinstance(delivery_id, str) else []
    return json.dumps({"delivery_id": delivery_id, "actions": records}, ensure_ascii=False)


if __name__ == "__main__":
    # Run standalone stdio server for MCP clients (Cursor, Claude Desktop, Antigravity)
    mcp.run()
